#!/usr/bin/env bash
# localtonet-watchdog.sh
#
# Kills/restarts hung or stale localtonet processes. Designed to be
# CONSERVATIVE: it must not disturb healthy processes.
#
# Two triggers, both requiring strong evidence:
#   (a) The process executable has been deleted from disk
#       (/proc/PID/exe -> "... (deleted)"). Unambiguous sign of a broken,
#       orphaned agent (this is what happened to the heuristic_elion
#       container that spun at 99.8% CPU for 2 days). Killed unconditionally.
#   (b) SUSTAINED high CPU: three consecutive recent-CPU samples (each ~3s)
#       all >= CPU_HIGH, AND the process is older than MIN_AGE_SECS (so we
#       never kill a young process that is legitimately busy during startup).
#       Recent CPU is computed from /proc/PID/stat deltas, NOT the lifetime
#       average that `ps -o %cpu` reports.
#
# The critical IAM service (systemd: Localtonet_MfYGm74ys.service, holds and
# auto-restores all tunnels) is NEVER killed with SIGKILL. If it itself meets
# the sustained-CPU trigger, we do a graceful `systemctl restart`, which is
# the documented reconnect path (see AGENTS.md "Localtonet tunnel (dev)").
#
# Run as root via systemd timer (see localtonet-watchdog.{service,timer}).
# Manual test:  sudo scripts/localtonet-watchdog.sh; tail /var/log/localtonet-watchdog.log
#
# Install:
#   sudo install -m 0755 scripts/localtonet-watchdog.sh /usr/local/sbin/
#   sudo install -m 0644 scripts/localtonet-watchdog.service scripts/localtonet-watchdog.timer /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now localtonet-watchdog.timer

LOG_FILE=${LOCALTONET_WATCHDOG_LOG:-/var/log/localtonet-watchdog.log}
CPU_HIGH=90.0
MIN_AGE_SECS=600
CPU_SAMPLE_SLEEP=3
CPU_CONFIRM_SAMPLES=3
IAM_UNIT=Localtonet_MfYGm74ys.service

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG_FILE"
}

# recent_cpu_pct <pid> -> prints recent CPU% over one ~3s window (0 if unknown)
recent_cpu_pct() {
  local pid=$1 rest utime0 stime0 utime1 stime1 hz t0 t1 dtick dtick_ns dsec_ns
  hz=$(getconf CLK_TCK 2>/dev/null) || hz=100

  rest=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null) || { echo 0; return; }
  read -r _ _ _ _ _ _ _ _ _ _ _ utime0 stime0 _ <<< "$rest" || { echo 0; return; }
  t0=$(date +%s%N)

  sleep "$CPU_SAMPLE_SLEEP"

  rest=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null) || { echo 0; return; }
  read -r _ _ _ _ _ _ _ _ _ _ _ utime1 stime1 _ <<< "$rest" || { echo 0; return; }
  t1=$(date +%s%N)

  dtick=$((utime1 + stime1 - utime0 - stime0))
  dsec_ns=$((t1 - t0))
  [ "$dsec_ns" -gt 0 ] || { echo 0; return; }
  awk -v d="$dtick" -v hz="$hz" -v ns="$dsec_ns" 'BEGIN { printf "%.1f", (d / hz) / (ns / 1e9) * 100 }'
}

# Skip ourselves (and any shell that wraps us).
MY_PIDS="$$ $PPID"

# The critical IAM service PID (excluded from SIGKILL; restarted via systemd).
IAM_PID=$(systemctl show -p MainPID --value "$IAM_UNIT" 2>/dev/null || echo 0)

pids=$(pgrep -f 'localtonet' 2>/dev/null || true)

for pid in $pids; do
  # Never consider the watchdog's own processes.
  case " $MY_PIDS " in *" $pid "*) continue ;; esac

  cmdline=$(cat "/proc/$pid/cmdline" 2>/dev/null | tr '\0' ' ') || continue
  [ -z "$cmdline" ] && continue

  # Skip anything named localtonet-watchdog.
  case "$cmdline" in *localtonet-watchdog*) continue ;; esac

  # (a) Deleted binary => broken/orphaned, kill unconditionally.
  exe=$(readlink "/proc/$pid/exe" 2>/dev/null)
  if [[ "$exe" == *" (deleted)"* ]]; then
    log "KILL pid=$pid deleted-binary ($cmdline)"
    kill -9 "$pid" 2>/dev/null && log "  killed" || log "  kill FAILED"
    continue
  fi

  # (b) Sustained high CPU. Only judge processes old enough to trust the sample.
  etime=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -n "$etime" ] || continue
  if [ "$etime" -lt "$MIN_AGE_SECS" ]; then
    continue
  fi

  high=0
  for _ in $(seq 1 "$CPU_CONFIRM_SAMPLES"); do
    cpu=$(recent_cpu_pct "$pid")
    if awk -v a="$cpu" -v t="$CPU_HIGH" 'BEGIN { exit (a >= t) ? 0 : 1 }'; then
      high=$((high + 1))
    else
      break
    fi
  done

  if [ "$high" -eq "$CPU_CONFIRM_SAMPLES" ]; then
    if [ "$pid" = "$IAM_PID" ]; then
      log "RESTART $IAM_UNIT sustained cpu>=$CPU_HIGH% ($cmdline)"
      systemctl restart "$IAM_UNIT" 2>/dev/null && log "  restarted" || log "  restart FAILED"
    else
      log "KILL pid=$pid sustained cpu>=$CPU_HIGH% x$high ($cmdline)"
      kill -9 "$pid" 2>/dev/null && log "  killed" || log "  kill FAILED"
    fi
  fi
done

exit 0

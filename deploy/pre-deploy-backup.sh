#!/usr/bin/env bash
#
# Pre-deploy snapshot for Sietch CRM production.
#
# Every artifact goes to the ATTACHED VOLUME (default /mnt/sietch_backup), never
# to the root disk. A full-data tarball is ~27 GB; writing that to the 77 GB root
# volume is what pushed the droplet to 92% on 2026-11-09, and a full disk takes
# the whole CRM down.
#
# The script refuses to run when the destination resolves to the root
# filesystem, and refuses to start when the destination is too full. Both are
# deliberate: a pre-deploy backup must never be the reason a deploy fails.
#
# Usage:
#   deploy/pre-deploy-backup.sh [commit-ish]
#
#   commit-ish defaults to HEAD (the running pre-deploy commit).
#
# Environment:
#   COMPOSE_DIR             app checkout           (default: /opt/sietch-crm)
#   PRE_DEPLOY_BACKUP_DIR   destination root       (default: /mnt/sietch_backup/pre-deploy)
#   PRE_DEPLOY_MIN_FREE_GB  required free space    (default: 35)
#   DASHBOARD_VOLUME        docker volume          (default: sietch-crm_dashboard-data)
#   UPLOADS_VOLUME          docker volume          (default: sietch-crm_dashboard-uploads)
#   DB_CONTAINER            postgres container     (default: sietch-db)
#   DB_USER / DB_NAME       database               (default: sietch / sietch_crm)
#   DRY_RUN=1               print the plan, write nothing
#
# Output layout (same as the pre-2026-11-09 ad-hoc backups):
#   <dest>/sietch-prod-pre-<sha>-<YYYYmmdd-HHMMSS>ET/
#     PRE_STATE.txt            commit, git status, docker ps
#     sietch_crm.dump          pg_dump -Fc  (contains CREATE DATABASE)
#     code-<sha>.tar.gz        git archive of the pre-deploy commit
#     dashboard-data.tar.gz    full data volume (profiles, attachments, ...)
#     dashboard-uploads.tar.gz uploads volume
#     env, docker-compose.yml, Dockerfile   host config not tracked in git
#     git-status-porcelain.txt
#
set -euo pipefail

COMPOSE_DIR=${COMPOSE_DIR:-/opt/sietch-crm}
DEST_ROOT=${PRE_DEPLOY_BACKUP_DIR:-/mnt/sietch_backup/pre-deploy}
MIN_FREE_GB=${PRE_DEPLOY_MIN_FREE_GB:-35}
DASHBOARD_VOLUME=${DASHBOARD_VOLUME:-sietch-crm_dashboard-data}
UPLOADS_VOLUME=${UPLOADS_VOLUME:-sietch-crm_dashboard-uploads}
DB_CONTAINER=${DB_CONTAINER:-sietch-db}
DB_USER=${DB_USER:-sietch}
DB_NAME=${DB_NAME:-sietch_crm}
DRY_RUN=${DRY_RUN:-0}

log() { printf '[%s] %s\n' "$(TZ=America/New_York date '+%F %T %Z')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

case "${1:-}" in
  -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

[ -d "$COMPOSE_DIR" ] || die "COMPOSE_DIR not found: $COMPOSE_DIR"

COMMIT=$(git -C "$COMPOSE_DIR" rev-parse --verify "${1:-HEAD}^{commit}" 2>/dev/null) \
  || die "not a valid commit in $COMPOSE_DIR: ${1:-HEAD}"
SHA=$(git -C "$COMPOSE_DIR" rev-parse --short "$COMMIT")
TS="$(TZ=America/New_York date +%Y%m%d-%H%M%S)ET"
DEST="$DEST_ROOT/sietch-prod-pre-$SHA-$TS"

# --- safety: never write a pre-deploy backup to the root filesystem ---------
mkdir -p "$DEST_ROOT"
root_dev=$(stat -c %d /)
dest_dev=$(stat -c %d "$DEST_ROOT")
if [ "$root_dev" = "$dest_dev" ]; then
  die "destination $DEST_ROOT is on the ROOT filesystem; refusing (a ~27 GB tarball here fills /)"
fi

free_gb=$(df -BG --output=avail "$DEST_ROOT" | tail -1 | tr -dc '0-9')
[ -n "$free_gb" ] || die "could not determine free space for $DEST_ROOT"
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  die "only ${free_gb}G free on $DEST_ROOT (need ${MIN_FREE_GB}G); free space first"
fi

log "pre-deploy backup of $SHA ($(git -C "$COMPOSE_DIR" log -1 --format=%s "$COMMIT"))"
log "destination: $DEST  (${free_gb}G free on $(df -h --output=target "$DEST_ROOT" | tail -1))"

if [ "$DRY_RUN" = 1 ]; then
  cat <<EOF
DRY_RUN plan (nothing written):
  1. mkdir                 $DEST
  2. pg_dump               docker exec $DB_CONTAINER pg_dump -U $DB_USER -d $DB_NAME -Fc > $DEST/sietch_crm.dump
  3. code archive          git -C $COMPOSE_DIR archive --format=tar.gz -o $DEST/code-$SHA.tar.gz $SHA
  4. data volume           docker run --rm -v $DASHBOARD_VOLUME:/data:ro -v $DEST:/backup alpine tar czf /backup/dashboard-data.tar.gz -C /data .
  5. uploads volume        docker run --rm -v $UPLOADS_VOLUME:/data:ro -v $DEST:/backup alpine tar czf /backup/dashboard-uploads.tar.gz -C /data .
  6. host config copies    .env, docker-compose.yml, Dockerfile, git status, PRE_STATE.txt
  destination is NOT on the root filesystem and has ${free_gb}G free.
EOF
  exit 0
fi

mkdir -p "$DEST"

log "1/6 pre-state"
{
  echo "PRE_COMMIT=$COMMIT"
  echo "CAPTURED=$(TZ=America/New_York date '+%F %T %Z')"
  echo
  echo "## git status --porcelain"
  git -C "$COMPOSE_DIR" status --porcelain
  echo
  echo "## docker ps"
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
} > "$DEST/PRE_STATE.txt"
git -C "$COMPOSE_DIR" status --porcelain > "$DEST/git-status-porcelain.txt"

log "2/6 database dump"
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$DEST/sietch_crm.dump" \
  || die "pg_dump failed"
chmod 600 "$DEST/sietch_crm.dump"

log "3/6 code archive"
git -C "$COMPOSE_DIR" archive --format=tar.gz -o "$DEST/code-$SHA.tar.gz" "$COMMIT" \
  || die "git archive failed"

log "4/6 dashboard-data volume (this is the large one — tens of GB)"
docker run --rm -v "$DASHBOARD_VOLUME":/data:ro -v "$DEST":/backup alpine \
  tar czf /backup/dashboard-data.tar.gz -C /data . || die "data volume backup failed"

log "5/6 uploads volume"
docker run --rm -v "$UPLOADS_VOLUME":/data:ro -v "$DEST":/backup alpine \
  tar czf /backup/dashboard-uploads.tar.gz -C /data . || die "uploads volume backup failed"

log "6/6 host config"
for f in .env docker-compose.yml Dockerfile; do
  [ -f "$COMPOSE_DIR/$f" ] || continue
  install -m 600 "$COMPOSE_DIR/$f" "$DEST/${f#.}"
done

log "done: $DEST"
du -sh "$DEST"/* 2>/dev/null | sort -rh
df -h "$DEST_ROOT" | tail -1

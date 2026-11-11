/**
 * Pure helpers for the Mail inbox and Edit Mail Account modals.
 *
 * These exist so the UI can skip redundant full-list redraws, ignore stale
 * async responses, coalesce mail:new bursts during a scanner sync, and refuse
 * a Save on the reused account-settings form until the loaded account matches
 * the form id. Unit-tested by tests/test_mail_modal_stability.mjs.
 */

export const MAIL_NEW_RELOAD_DEBOUNCE_MS = 400;

export function mailListSignature(msgs, page, total) {
  const rows = (msgs || []).map((m) => {
    const id = m && (m.id != null ? m.id : m.ID);
    const read = m && (m.read || m.isRead || m.seen) ? "1" : "0";
    const star = m && (m.starred || m.flagged) ? "1" : "0";
    const subj = m && (m.subject || m.Subject || "");
    const fail = m && m.sync_failure ? "1" : "0";
    return `${id}:${read}:${star}:${subj}:${fail}`;
  });
  return `${page || 1}|${total || 0}|${rows.join(",")}`;
}

export function shouldSkipMailListRender(prevSig, nextSig) {
  return Boolean(prevSig) && prevSig === nextSig;
}

export function mailTabsSignature(accounts, activeAccount, isAdmin) {
  const tabs = (accounts || [])
    .filter((a) => a && a.is_crm_mail !== true)
    .map((a) => `${a.id}:${a.display_name || a.email || ""}:${a.tab_icon || ""}:${a.tab_color || ""}:${a.authType || ""}`);
  return `crm|${tabs.join("|")}|active=${activeAccount == null ? "" : activeAccount}|admin=${isAdmin ? "1" : "0"}`;
}

export function accountSettingsSaveIsAllowed(formId, loadedId) {
  if (formId == null || formId === "") return true;
  if (loadedId == null || loadedId === "") return false;
  return Number(formId) === Number(loadedId);
}

/**
 * Trailing coalescer: many schedule() calls in a burst produce one run
 * after `waitMs` of quiet. Exposes stats so tests can assert a bounded
 * render/run count.
 */
export function createCoalescedRunner(fn, waitMs) {
  let timer = null;
  let calls = 0;
  let runs = 0;
  function schedule(...args) {
    calls += 1;
    if (timer != null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      runs += 1;
      fn(...args);
    }, waitMs);
  }
  schedule.stats = () => ({ calls, runs, pending: timer != null });
  schedule.cancel = () => {
    if (timer != null) {
      clearTimeout(timer);
      timer = null;
    }
  };
  return schedule;
}

export function createGenerationGate() {
  let gen = 0;
  return {
    next() {
      gen += 1;
      return gen;
    },
    isCurrent(g) {
      return g === gen;
    },
    get current() {
      return gen;
    },
  };
}

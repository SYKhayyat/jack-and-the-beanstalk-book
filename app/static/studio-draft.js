/* The studio's memory in this browser, one entry per story.

   What lives here is only ids and choices — which draft each section chose,
   its settings, whether it is folded — never a photo. The photos and comics
   stay server-side as 24-hour drafts, so nothing of a child's face is kept in
   the browser, and a stale entry is harmless: the server simply says the
   draft is gone.

   Storage can be missing or throw (private windows, blocked site data), so
   every access is guarded and the studio still works, just without memory. */

const VERSION = 1;

function storageKey(story) {
  return `studio:${story}`;
}

function empty() {
  return { v: VERSION, sections: {}, about: {} };
}

export function loadDraft(story) {
  try {
    const raw = window.localStorage.getItem(storageKey(story));
    if (!raw) return empty();
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.v !== VERSION || typeof parsed.sections !== "object") return empty();
    parsed.about = parsed.about || {};
    return parsed;
  } catch {
    return empty();
  }
}

export function saveDraft(story, draft) {
  try {
    window.localStorage.setItem(storageKey(story), JSON.stringify(draft));
  } catch {
    /* no storage: the studio keeps working for this visit */
  }
}

export function forgetDraft(story) {
  try {
    window.localStorage.removeItem(storageKey(story));
  } catch {
    /* nothing to forget */
  }
}

/* The draft id the book will use for a section: the chosen take, if any. */
export function chosenId(section) {
  return section?.chosenId || null;
}

/* The shelf: the one-time face hint, and "Keep going" when — and only when —
   this browser's studio draft for a story still exists on the server. */

import { createStudioClient } from "./api.js";
import { chosenId, forgetDraft, loadDraft } from "./studio-draft.js";

const HINT_KEY = "shelf:face-hint-seen";
const studio = createStudioClient();

showHintOnce();
for (const card of document.querySelectorAll(".story-card[data-story]")) {
  void markKeepGoing(card);
}

function showHintOnce() {
  const hint = document.getElementById("face-hint");
  if (!hint) return;
  let seen = false;
  try {
    seen = window.localStorage.getItem(HINT_KEY) === "1";
  } catch {
    /* no storage: show it, it is only a sentence */
  }
  if (seen) return;
  hint.classList.remove("hidden");
  document.getElementById("face-hint-dismiss")?.addEventListener("click", () => {
    hint.classList.add("hidden");
    try {
      window.localStorage.setItem(HINT_KEY, "1");
    } catch {
      /* fine */
    }
  });
}

/* A draft older than a day is gone server-side. Saying "Keep going" and then
   opening a studio full of expired faces would be a small lie, so the label
   changes only after the server confirms the star's draft is still there;
   a stale draft is forgotten instead. */
async function markKeepGoing(card) {
  const state = card.dataset.state;
  if (state !== "fresh" && state !== "finished") return;
  const story = card.dataset.story;
  const draft = loadDraft(story);
  const starId = chosenId(draft.sections.star);
  if (!starId) return;
  if (!(await studio.draftAlive(starId))) {
    forgetDraft(story);
    return;
  }
  const action = card.querySelector("[data-card-action]");
  if (action) action.textContent = "Keep going ▸";
  card.dataset.state = "resume";
}

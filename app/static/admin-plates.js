/* Paint a story's missing plates without 21 trips through the page.

   The server paints exactly one plate per request — a whole story is minutes of
   image calls, which no single request should carry. So this loop presses the
   same button the form posts to, one plate at a time, and reports where it got
   to. Without JavaScript the form still works: one press, one plate, and the
   sheet stays on screen with its own count. */

import { paintMissingPlate } from "./api.js";

const form = document.querySelector("[data-paint]");
if (form) {
  const button = form.querySelector("button");
  const status = form.querySelector("[data-paint-status]");
  const story = form.dataset.story;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    const remainingLine = status.textContent.trim();
    if (!window.confirm(`${remainingLine}\n\nEach call costs money. Continue?`)) return;

    button.disabled = true;
    let painted = 0;
    try {
      for (;;) {
        const result = await paintMissingPlate(story);
        if (result.page === null || result.page === undefined) break;
        painted += 1;
        const done = result.total - result.remaining;
        status.textContent = `Painted page ${result.page}. ${done} of ${result.total} plates painted…`;
      }
      window.location.reload();
    } catch (error) {
      status.textContent =
        painted > 0
          ? `Stopped after ${painted} plate(s): ${error.message || "the provider failed"}. Press again to carry on.`
          : error.message || "The plate could not be painted.";
      button.disabled = false;
    }
  });
}

/* Book controller — event wiring and orchestration for the book page.
   No DOM access of its own (element references come from book-view), no state
   clumps (changes go through book-state), no HTTP (all calls go through the
   client in api.js). */

import { createCameraSource, createFilePickerSource, isTouchDevice } from "./photo.js";
import { createBookClient, postForm } from "./api.js";
import { preparePhoto, UnreadableImageError } from "./downscale.js";
import * as state from "./book-state.js";
import * as view from "./book-view.js";

const MAX_UPLOAD = Number(document.body.dataset.maxUploadMb || 10) * 1024 * 1024;
const POLL_MS = 1500;
const MAX_QUIET_POLLS = 4; // consecutive failed polls before we give up visibly

const bookApi = createBookClient();
const book = state.createBookState();
const photoInput = view.elements.photo();
const filePicker = createFilePickerSource(photoInput);
const camera = createCameraSource(view.elements.cameraVideo());

/* Story-page total. Seeded with the full-tale count (19 story pages, then the
   About page in the PDF) and then corrected from the server's own page list,
   so the first progress line cannot lie. */
let totalPages = 19;
seedTotalPages();

wirePhotoInput();
wireCamera();
wirePortraitFlow();
wireBookFlow();

/* --- the photo ----------------------------------------------------------- */

function wirePhotoInput() {
  photoInput.addEventListener("change", () => void acceptPhoto());
  if (isTouchDevice()) filePicker.useNativeCameraOnTouch(true);
}

async function acceptPhoto(captured = null) {
  /* A captured camera frame is handed in directly; without one we take the
     picker's file. Never re-derive "camera vs picker" after closing the
     camera — by then the stream is gone and the picker holds a stale file. */
  const blob = captured ?? (camera.active ? camera.getBlob() : filePicker.getBlob());
  if (!blob) {
    view.showPhotoMessage("Choose a photo first.", "error");
    return;
  }
  try {
    view.showPhotoMessage("Preparing your photo…");
    const prepared = await preparePhoto(blob, { maxBytes: MAX_UPLOAD });
    state.setPhoto(book, prepared.blob, blob.name || "photo.jpg");
    view.renderPhotoPreview(book.photo);
    view.showPhotoMessage(prepared.scaled ? "Photo shrunk to fit." : "");
    view.setBusyPortrait("");
    view.hideReview();
    view.resetBookStage();
  } catch (error) {
    if (error instanceof UnreadableImageError) {
      view.showPhotoMessage("That file is not a readable photo — try another.", "error");
    } else {
      view.showPhotoMessage("The photo could not be prepared — try another.", "error");
    }
  }
}

/* --- the camera ---------------------------------------------------------- */

/* Says why the camera did not open instead of echoing a raw error string. */
function cameraOpenMessage(error) {
  if (error.name === "NotAllowedError") {
    return "The browser blocked the camera — allow it in the site settings, or use a photo from the picker.";
  }
  if (error.name === "NotFoundError") {
    return "No camera found on this device — use a photo from the picker instead.";
  }
  if (error.name === "NotSupportedError" || !navigator.mediaDevices) {
    return "This browser has no camera support — use a photo from the picker instead.";
  }
  return "The camera could not open — the file picker still works. " + error.message;
}

function wireCamera() {
  view.elements.useCamera().addEventListener("click", async () => {
    try {
      await camera.open();
      view.showCameraPane(true);
      view.showCameraMessage("");
    } catch (error) {
      view.showCameraMessage(cameraOpenMessage(error));
    }
  });
  view.elements.cameraShutter().addEventListener("click", () => {
    camera
      .capture()
      .then((frame) => {
        camera.close();
        view.showCameraPane(false);
        return acceptPhoto(frame);
      })
      .catch((error) => view.showCameraMessage(error.message));
  });
  view.elements.cameraCancel().addEventListener("click", () => {
    camera.close();
    view.showCameraPane(false);
  });
}

/* --- portrait: make, review, approve ------------------------------------- */

function wirePortraitFlow() {
  const form = view.elements.form();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (camera.active) {
      view.showCameraMessage("Close the camera before continuing — cancel or take the picture.");
      return;
    }
    void cartoonify();
  });
  view.elements.approve().addEventListener("click", () => approveAndStart());
  view.elements.reject().addEventListener("click", () => view.showRejectBody(!view.rejectBodyOpen()));
  view.elements.rejectRetry().addEventListener("click", () => void retryPortrait());
}

/* --- helpers ------------------------------------------------------------- */

/* The portrait pass reuses /api/cartoonify with default server settings for
   anything the book form does not offer; only the fields we ship may differ.
   `reasons` are the ticked rejection chips, sent as repeated `reason` fields
   for closed-set retry feedback. postForm() appends the photo once (settingsForm
   owns it) and throws on non-ok responses, mapping the server's `detail`. */
function settingsForm(reasons = []) {
  const form = view.readCharacterSettings();
  form.append("photo", book.photo, book.photoName);
  for (const reason of reasons) form.append("reason", reason);
  return form;
}

async function cartoonify(reasons = []) {
  if (!book.photo) {
    view.showPhotoMessage("Choose a photo first.", "error");
    return;
  }
  view.setBusyPortrait(reasons.length ? "Making a better portrait…" : "Making the portrait…");
  try {
    const payload = await postForm("/api/cartoonify", settingsForm(reasons));
    state.setDraft(book, payload.generation_id);
    if (view.oneStepChecked()) {
      // Fast mode: skip the preview, start the book right away.
      view.setBusyPortrait("");
      await approveAndStart();
      return;
    }
    view.showReview(
      payload.images.cartoon_url,
      reasons.length
        ? "A new portrait, with the fixes you ticked."
        : "This portrait will appear on every page of the book.",
    );
    view.showPrompts({ cartoonPrompt: payload.prompts?.cartoon || "", bookId: book.bookId });
    if (reasons.length) view.showRejectBody(false);
  } catch (error) {
    view.setBusyPortrait("");
    view.showPhotoMessage(error.message || "The portrait did not come out — try again.", "error");
  }
}

/* "Not right" opens the chip panel; the retry inside it re-runs the portrait
   with the ticked faults as corrections. With nothing ticked the state simply
   resets to the form, exactly as before chips existed. */
function rejectPortrait() {
  view.showRejectBody(true);
}

async function retryPortrait() {
  const reasons = view.rejectChipValues();
  view.showRejectBody(false);
  view.resetRejectChips();
  state.retryPortrait(book);
  view.hideReview();
  await cartoonify(reasons);
}

/* --- the book ------------------------------------------------------------ */

function seedTotalPages() {
  fetch("/api/book/pages")
    .then((response) => (response.ok ? response.json() : null))
    .then((catalogue) => {
      if (catalogue && Array.isArray(catalogue.pages) && catalogue.pages.length) {
        totalPages = catalogue.pages.length;
      }
    })
    .catch(() => {
      /* the seed stays until the poll's first payload corrects it */
    });
}

/* Each optional character photo gets the same cheap portrait pass the star
   does: a cartoon of that face, made before the pages (the expensive part).
   The page prompt carries the character's pinned look, so this only supplies
   the face. Only photoable slots exist in the DOM; the cow has none. */
async function characterReferences() {
  const entries = [];
  for (const input of view.characterInputs()) {
    const file = input.files?.[0];
    if (!file) continue;
    const prepared = await preparePhoto(file, { maxBytes: MAX_UPLOAD });
    const form = new FormData();
    form.append("photo", prepared.blob, file.name || "character.jpg");
    const payload = await postForm("/api/cartoonify", form);
    entries.push([input.dataset.character, payload.generation_id]);
  }
  return entries;
}

async function approveAndStart() {
  if (book.pendingDraft) state.approveDraft(book);
  if (!state.isApproved(book)) {
    view.showReviewMessage("Make a portrait first — choose a photo and press the button.", "error");
    return;
  }
  const about = view.readAboutMe();
  view.elements.approve().disabled = true;
  view.showReviewMessage("Starting the book…");
  const form = new FormData();
  form.append("generation_id", book.generationId);
  for (const [key, value] of Object.entries(about)) {
    if (value !== null && value !== "") form.append(key, String(value));
  }
  try {
    for (const [key, generationId] of await characterReferences()) {
      form.append("character", `${key}:${generationId}`);
    }
    const payload = await bookApi.start(form);
    state.setBook(book, payload.book_id);
    view.showReviewMessage("");
    // The book now has an id: make its prompts.json inspectable.
    view.showPrompts({ bookId: payload.book_id });
    view.showBookStage();
    view.renderBookPages([]);
    view.renderBookProgress(0, totalPages);
    pollBook();
  } catch (error) {
    view.elements.approve().disabled = false;
    view.showReviewMessage(
      error.message || "Could not reach the server — try again.",
      "error",
    );
  }
}

function pollBook(failures = 0) {
  window.setTimeout(async () => {
    if (!book.bookId || state.isBookFinished(book)) return;
    const expectedBook = book.bookId;
    try {
      const payload = await bookApi.status(expectedBook);
      // A new photo may have reset the state while this poll was in flight.
      // Its answer must not paint over the fresh book.
      if (book.bookId !== expectedBook) return;
      state.applyStatus(book, payload);
      if (payload.pages?.length) totalPages = payload.pages.length;
      const done = book.pages.filter((page) => page.status === "done").length;
      const total = book.pages.length || totalPages;
      view.renderBookPages(book.pages);
      if (state.isBookFailed(book)) {
        view.showBookError(book.error || "The book failed. Try again.");
        view.showRestart();
        return;
      }
      if (state.isBookFinished(book)) {
        // The finished book is the PDF itself, shown in the big viewer; the
        // link below it downloads the same file.
        view.renderBookFinished(book.bookId, `/book-media/${book.bookId}/pdf`);
        return;
      }
      view.renderBookProgress(done, total);
      pollBook();
    } catch {
      /* transient network hiccup: retry quietly, but not forever. The count
         must travel with the retry — a fresh `started` per call could never
         accumulate four consecutive failures. */
      if (book.bookId !== expectedBook) return;
      const failedPolls = failures + 1;
      if (failedPolls < MAX_QUIET_POLLS) {
        pollBook(failedPolls);
        return;
      }
      view.showBookError("The book cannot be reached right now — keep this tab open.");
      view.showRestart();
    }
  }, POLL_MS);
}

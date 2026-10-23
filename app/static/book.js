/* Book controller — event wiring and orchestration for /book only.
   No DOM access of its own (element references come from book-view), no state
   clumps (changes go through book-state), no HTTP (all calls go through the
   client in api.js). Photo sources and downscaled photos are shared with the
   single-scene page, so the same seams hold. */

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

/* Story-page total. Seeded (four beats today) and then corrected from the
   server's own page list, so the first progress line cannot lie. */
let totalPages = 4;
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
    view.showPhotoMessage(prepared.scaled ? "Photo shrunk for the image model." : "");
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

/* Same taxonomy the single-scene page uses, so both pages say why the camera
   did not open instead of echoing a raw error string. */
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
  view.elements.reject().addEventListener("click", () => rejectPortrait());
}

/* --- helpers ------------------------------------------------------------- */

/* The portrait pass reuses /api/cartoonify with default server settings for
   anything the book form does not offer; only the fields we ship may differ.
   postForm() appends the photo once (settingsForm owns it) and throws on
   non-ok responses, mapping the server's `detail` into the message. */
function settingsForm() {
  const form = view.readCharacterSettings();
  form.append("photo", book.photo, book.photoName);
  return form;
}

async function cartoonify() {
  if (!book.photo) {
    view.showPhotoMessage("Choose a photo first.", "error");
    return;
  }
  view.setBusyPortrait("Making the portrait…");
  try {
    const payload = await postForm("/api/cartoonify", settingsForm());
    state.setDraft(book, payload.generation_id);
    view.showReview(
      payload.images.cartoon_url,
      "This portrait will appear on every page of the book.",
    );
  } catch (error) {
    view.setBusyPortrait("");
    view.showPhotoMessage(error.message || "The portrait did not come out — try again.", "error");
  }
}

function rejectPortrait() {
  state.retryPortrait(book);
  view.hideReview();
  view.setBusyPortrait("Try again with different settings below.");
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

async function approveAndStart() {
  if (book.pendingDraft) state.approveDraft(book);
  if (!state.isApproved(book)) {
    view.showReviewMessage("Make a portrait first — choose a photo and press Step 4.", "error");
    return;
  }
  const about = view.readAboutMe();
  if (!about.child_name) {
    view.showReviewMessage("The child's name is needed for the About Me page.", "error");
    return;
  }
  view.elements.approve().disabled = true;
  view.showReviewMessage("Starting the book…");
  const form = new FormData();
  form.append("generation_id", book.generationId);
  for (const [key, value] of Object.entries(about)) {
    if (value !== null && value !== "") form.append(key, String(value));
  }
  try {
    const payload = await bookApi.start(form);
    state.setBook(book, payload.book_id);
    view.showReviewMessage("");
    view.showBookStage();
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

function pollBook() {
  const started = Date.now();
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
      view.renderBookPages(book);
      if (state.isBookFailed(book)) {
        view.showBookError(book.error || "The book failed. Try again.");
        view.elements.restart().classList.remove("hidden");
        return;
      }
      if (state.isBookFinished(book)) {
        view.renderBookFinished(book.bookId, `/book-media/${book.bookId}/pdf`);
        // The last status update may not have shown every page — refresh once.
        await refreshStatusOnce();
        return;
      }
      view.renderBookProgress(done, total);
      pollBook();
    } catch {
      /* transient network hiccup: retry quietly, but not forever */
      if (book.bookId !== expectedBook) return;
      if (Date.now() - started < MAX_QUIET_POLLS * POLL_MS) {
        pollBook();
        return;
      }
      view.showBookError("The book cannot be reached right now — keep this tab open.");
      view.elements.restart().classList.remove("hidden");
    }
  }, POLL_MS);
}

async function refreshStatusOnce() {
  try {
    const payload = await bookApi.status(book.bookId);
    state.applyStatus(book, payload);
    view.renderBookPages(book);
  } catch {
    /* the PDF link is already live; the page grid is best-effort */
  }
}

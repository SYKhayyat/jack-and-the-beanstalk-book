/* Book controller — event wiring and orchestration for /book only.
   No DOM access of its own (element references come from book-view), no state
   clumps (changes go through book-state), no HTTP beyond the small client
   below. Photo sources and downscaled photos are shared with the single-scene
   page, so the same seams hold. */

import { createCameraSource, createFilePickerSource, isTouchDevice } from "./photo.js";
import { preparePhoto, UnreadableImageError } from "./downscale.js";
import * as state from "./book-state.js";
import * as view from "./book-view.js";

const MAX_UPLOAD = Number(document.body.dataset.maxUploadMb || 10) * 1024 * 1024;
const POLL_MS = 1500;

const book = state.createBookState();
const photoInput = view.elements.photo();
const filePicker = createFilePickerSource(photoInput);
const camera = createCameraSource(view.elements.cameraVideo());

wirePhotoInput();
wireCamera();
wirePortraitFlow();
wireBookFlow();

/* --- the photo ----------------------------------------------------------- */

function wirePhotoInput() {
  photoInput.addEventListener("change", () => void acceptPhoto());
  if (isTouchDevice()) filePicker.useNativeCameraOnTouch(true);
}

async function acceptPhoto() {
  const source = camera.active ? camera : filePicker;
  const blob = source.getBlob();
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
      throw error;
    }
  }
}

/* --- the camera ---------------------------------------------------------- */

function wireCamera() {
  view.elements.useCamera().addEventListener("click", async () => {
    try {
      await camera.open();
      view.showCameraPane(true);
      view.showCameraMessage("");
    } catch (error) {
      view.showCameraMessage(
        "The camera could not open — the file picker still works. " + error.message,
      );
    }
  });
  view.elements.cameraShutter().addEventListener("click", () => {
    camera
      .capture()
      .then(() => camera.close())
      .then(() => {
        view.showCameraPane(false);
        return acceptPhoto();
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
   anything the book form does not offer; only the fields we ship may differ. */
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
    const form = settingsForm();
    form.append("photo", book.photo, book.photoName);
    const response = await fetch("/api/cartoonify", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) {
      view.setBusyPortrait("");
      view.showPhotoMessage(payload.detail || "The portrait did not come out — try again.", "error");
      return;
    }
    state.setDraft(book, payload.generation_id);
    view.showReview(
      payload.images.cartoon_url,
      "This portrait will appear on every page of the book.",
    );
  } catch {
    view.setBusyPortrait("");
    view.showPhotoMessage("Could not reach the server — check the connection and retry.", "error");
  }
}

function rejectPortrait() {
  state.retryPortrait(book);
  view.hideReview();
  view.setBusyPortrait("Try again with different settings below.");
}

/* --- the book ------------------------------------------------------------ */


async function approveAndStart() {
  if (!state.isApproved(book)) return;
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
    const response = await fetch("/api/book/start", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) {
      view.elements.approve().disabled = false;
      view.showReviewMessage(payload.detail || "The book could not start.", "error");
      return;
    }
    state.setBook(book, payload.book_id);
    view.showReviewMessage("");
    view.showBookStage();
    view.renderBookProgress(0, 3);
    pollBook();
  } catch {
    view.elements.approve().disabled = false;
    view.showReviewMessage("Could not reach the server — try again.", "error");
  }
}

function pollBook() {
  window.setTimeout(async () => {
    if (!book.bookId || state.isBookFinished(book)) return;
    try {
      const response = await fetch(`/api/book/${book.bookId}`);
      if (!response.ok) {
        view.showBookError("The book could not be found — start a new one.");
        return;
      }
      const payload = await response.json();
      state.applyStatus(book, payload);
      const done = book.pages.filter((page) => page.status === "done").length;
      const total = book.pages.length || 1;
      view.renderBookPages(book);
      if (state.isBookFailed(book)) {
        view.showBookError(book.error || "The book failed. Try again.");
        view.elements.restart().classList.remove("hidden");
        return;
      }
      if (state.isBookFinished(book)) {
        const pdfUrl = `/api/book/${book.bookId}/pdf`;
        view.renderBookFinished(book.bookId, pdfUrl);
        // The last status update may not have shown every page — refresh once.
        await refreshStatusOnce();
        return;
      }
      view.renderBookProgress(done, total);
      pollBook();
    } catch {
      pollBook(); // transient network hiccup: keep trying quietly
    }
  }, POLL_MS);
}

async function refreshStatusOnce() {
  try {
    const response = await fetch(`/api/book/${book.bookId}`);
    if (response.ok) {
      state.applyStatus(book, await response.json());
      view.renderBookPages(book);
    }
  } catch {
    /* the PDF link is already live; the page grid is best-effort */
  }
}

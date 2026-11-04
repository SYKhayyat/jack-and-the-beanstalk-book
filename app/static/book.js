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
const characterCamera = createCameraSource(view.elements.characterCameraVideo());

/* Camera-captured character photos, keyed by cast slot. A captured frame is
   used in preference to the slot's file input, exactly as for the star. */
const characterPhotos = new Map();
let characterCameraTarget = null;

/* Story-page total. Seeded with the full-tale count (19 story pages plus the
   cover, which is a page of the book too) and then corrected from the server's
   own page list, so the first progress line cannot lie. */
let totalPages = 20;
seedTotalPages();

wirePhotoInput();
wireCamera();
wireCharacterCameras();
wirePortraitFlow();
view.restartLink().addEventListener("click", () => view.forgetBook());

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

/* Each extra cast slot gets the same in-page camera as the star, with one
   shared pane re-targeted to the slot whose button was pressed. The slot also
   accepts a picked file (previewed like a camera frame) and offers its own
   "Make the cartoon" pass, so the face can be judged before the book starts. */
function wireCharacterCameras() {
  for (const input of view.characterInputs()) {
    const key = input.dataset.character;
    view.useCharacterCamera(key).addEventListener("click", async () => {
      try {
        await characterCamera.open();
        characterCameraTarget = key;
        view.setCharacterCameraTarget(view.characterLabel(key));
        view.showCharacterCameraPane(true);
        view.showCharacterCameraMessage("");
      } catch (error) {
        view.showCharacterMessage(key, cameraOpenMessage(error), "error");
      }
    });
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (file) void acceptCharacterPhoto(key, file);
    });
    view
      .makeCharacterCartoon(key)
      .addEventListener("click", () => void requestCharacterCartoon(key));
  }
  view.elements.characterCameraShutter().addEventListener("click", () => {
    const key = characterCameraTarget;
    if (!key) return;
    characterCamera
      .capture()
      .then((frame) => {
        characterCamera.close();
        view.showCharacterCameraPane(false);
        return acceptCharacterPhoto(key, frame);
      })
      .catch((error) => view.showCharacterCameraMessage(error.message));
  });
  view.elements.characterCameraCancel().addEventListener("click", () => {
    characterCamera.close();
    view.showCharacterCameraPane(false);
    characterCameraTarget = null;
  });
}

async function acceptCharacterPhoto(key, photo) {
  try {
    view.showCharacterMessage(key, "Preparing the photo…");
    const prepared = await preparePhoto(photo, { maxBytes: MAX_UPLOAD });
    characterPhotos.set(key, prepared.blob);
    view.renderCharacterPreview(key, prepared.blob);
    // A new photo invalidates any cartoon made from the old one.
    state.clearCharacterDraft(book, key);
    view.hideCharacterCartoon(key);
    view.setCharacterCartoonButton(key, {
      disabled: false,
      label: "Make the cartoon",
    });
    view.showCharacterMessage(key, prepared.scaled ? "Photo shrunk to fit." : "");
  } catch (error) {
    view.showCharacterMessage(
      key,
      error instanceof UnreadableImageError
        ? "That frame is not a readable photo — try again."
        : "The photo could not be prepared — try again.",
      "error",
    );
  }
}

/* The character's own portrait pass. It sends the cast key with the photo, so
   the server draws that character instead of the book's child, and returns a
   draft that the start endpoint will accept for that slot only. */
async function requestCharacterCartoon(key, { photo = null, silent = false } = {}) {
  const blob = photo || characterPhotos.get(key);
  if (!blob) {
    if (!silent) view.showCharacterMessage(key, "Choose a photo first.", "error");
    return null;
  }
  view.setCharacterCartoonButton(key, { disabled: true, label: "Making the cartoon…" });
  if (!silent) view.showCharacterMessage(key, "");
  const form = settingsFields();
  form.append("photo", blob, `${key}.jpg`);
  form.append("character", key);
  try {
    const payload = await postForm("/api/cartoonify", form);
    state.setCharacterDraft(book, key, payload.generation_id, payload.images.cartoon_url);
    view.renderCharacterCartoon(key, payload.images.cartoon_url);
    view.setCharacterCartoonButton(key, { disabled: false, label: "Make another cartoon" });
    view.showCharacterMessage(key, "");
    return payload.generation_id;
  } catch (error) {
    const existing = state.characterDraft(book, key);
    view.setCharacterCartoonButton(key, {
      disabled: false,
      label: existing ? "Make another cartoon" : "Make the cartoon",
    });
    if (!sessionGone(error)) {
      view.showCharacterMessage(
        key,
        error.message || "The cartoon did not come out — try again.",
        "error",
      );
    }
    return null;
  }
}

/* --- portrait: make, review, approve ------------------------------------- */

function wirePortraitFlow() {
  const form = view.elements.form();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (camera.active || characterCamera.active) {
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
/* A session that expired mid-flow is not an error message to show: it is a
   sign-in to do, and the book (if it exists) must survive the detour. */
function sessionGone(error) {
  if (error.status !== 401) return false;
  const next = book.bookId ? `/books/${book.bookId}` : "/";
  view.redirectTo(`/login?next=${next}`);
  return true;
}

function settingsFields(reasons = []) {
  const form = view.readCharacterSettings();
  for (const reason of reasons) form.append("reason", reason);
  return form;
}

function settingsForm(reasons = []) {
  const form = settingsFields(reasons);
  form.append("photo", book.photo, book.photoName);
  return form;
}

async function cartoonify(reasons = []) {
  if (!book.photo) {
    view.showPhotoMessage("Choose a photo first.", "error");
    return;
  }
  state.beginPortrait(book);
  view.setBusyPortrait(reasons.length ? "Making a better portrait…" : "Making the portrait…");
  // The review's approve button must not be clickable while a new portrait is
  // still being drawn: the old draft is about to be replaced.
  view.setReviewBusy(true, reasons.length ? "Making a better portrait…" : "Making the portrait…");
  try {
    const payload = await postForm("/api/cartoonify", settingsForm(reasons));
    state.setDraft(book, payload.generation_id);
    if (view.oneStepChecked()) {
      // Fast mode: skip the preview, start the book right away. The portrait
      // is finished at this point, so it must not still look "being drawn".
      state.endPortrait(book);
      view.setBusyPortrait("");
      view.setReviewBusy(false);
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
    if (sessionGone(error)) return;
    const message = error.message || "The portrait did not come out — try again.";
    // The user is looking at the review stage, so the error belongs there —
    // the photo field's message is scrolled out of sight.
    if (view.reviewVisible()) view.showReviewMessage(message);
    else view.showPhotoMessage(message, "error");
  } finally {
    state.endPortrait(book);
    view.setBusyPortrait("");
    if (view.reviewVisible()) view.setReviewBusy(false);
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
  // The current portrait is deliberately kept: if the retry fails, the user
  // can still approve what they already have. The review stays visible with
  // its buttons disabled until the new portrait lands.
  await cartoonify(reasons);
}

/* --- the book ------------------------------------------------------------ */

function seedTotalPages() {
  fetch("/api/book/pages")
    .then((response) => (response.ok ? response.json() : null))
    .then((catalogue) => {
      if (catalogue && Array.isArray(catalogue.pages) && catalogue.pages.length) {
        // The catalogue holds the story pages only; the cover is page one of
        // the book, so the count the reader sees is one higher.
        totalPages = catalogue.pages.length + 1;
      }
    })
    .catch(() => {
      /* the seed stays until the poll's first payload corrects it */
    });
}

/* Every filled character slot needs a cartoon before the book starts. Slots
   whose cartoon was already made with the "Make the cartoon" button are
   reused; any still missing one is made now, visibly, because that call can
   take a while. A photo picked but not yet prepared is prepared here too. */
async function characterReferences() {
  const entries = [];
  for (const input of view.characterInputs()) {
    const key = input.dataset.character;
    let blob = characterPhotos.get(key);
    if (!blob && input.files?.[0]) {
      const prepared = await preparePhoto(input.files[0], { maxBytes: MAX_UPLOAD });
      blob = prepared.blob;
      characterPhotos.set(key, blob);
    }
    if (!blob) continue;
    if (!state.characterDraft(book, key)) {
      view.showCharacterMessage(key, "Making the cartoon…");
      const generationId = await requestCharacterCartoon(key, { photo: blob, silent: true });
      if (!generationId) {
        throw new Error(
          `The ${view.characterLabel(key)} cartoon could not be made — try again.`,
        );
      }
    }
    entries.push([key, state.characterDraft(book, key).generationId]);
  }
  return entries;
}

async function approveAndStart() {
  if (state.isBookInProgress(book)) {
    // A double click, or "approve" pressed in a stale tab, must not start a
    // second book over the one already running.
    view.showReviewMessage("The book is already being made — watch the pages below.", "error");
    return;
  }
  if (book.portraitBusy) {
    view.showReviewMessage(
      "The portrait is still being made — one moment, then press this again.",
      "error",
    );
    return;
  }
  if (book.pendingDraft) state.approveDraft(book);
  if (!state.isApproved(book)) {
    view.showReviewMessage("Make a portrait first — choose a photo and press the button.", "error");
    return;
  }
  const about = view.readAboutMe();
  view.elements.approve().disabled = true;
  view.showReviewMessage("Starting the book…");
  // The waiting starts here: the character cartoons are made before the book
  // is queued, and that can take a while, so the reader gets a progress line
  // straight away rather than a frozen button.
  view.showBookStage();
  view.renderBookPages([]);
  view.renderBookProgress(0, totalPages, "starting");
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
    view.rememberBook(payload.book_id);
    view.showReviewMessage("");
    // The book now has an id: make its prompts.json inspectable.
    view.showPrompts({ bookId: payload.book_id });
    view.renderBookProgress(0, totalPages, book.phase);
    pollBook();
  } catch (error) {
    view.elements.approve().disabled = false;
    if (sessionGone(error)) return;
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
      view.renderBookProgress(done, total, book.phase);
      pollBook();
    } catch (error) {
      if (book.bookId !== expectedBook) return;
      if (error.status === 401) {
        // The session went away mid-render. Keep the book id, sign in, and
        // come straight back to this book instead of losing the page.
        view.rememberBook(expectedBook);
        view.redirectTo(`/login?next=/books/${expectedBook}`);
        return;
      }
      /* transient network hiccup: retry quietly, but not forever. The count
         must travel with the retry — a fresh `started` per call could never
         accumulate four consecutive failures. */
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

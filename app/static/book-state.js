/* Book model — pure data, no DOM.

   Lifecycle: photo -> portrait draft (generation_id) -> approved -> book job
   (book_id) -> pages landing -> PDF. Every transition writes through here;
   the controller wires events and the view renders from this state. */

export function createBookState() {
  return {
    /* The chosen photo, ready to POST. */
    photo: null,
    photoName: "photo.jpg",
    /* Pass 1: the approved portrait draft. */
    generationId: null,
    pendingDraft: null, // a fresh portrait waiting for approval
    /* True while a portrait call is in flight, so the controller can refuse
       to start a book on a portrait that is still being drawn. */
    portraitBusy: false,
    /* Per-slot cast portraits already made: cast key -> {generationId, cartoonUrl}.
       Reused when the book starts; only missing slots are made then. */
    characterDrafts: {},
    /* The running book job. */
    bookId: null,
    pages: [],
    bookStatus: null,
    phase: null,
    error: null,
    busy: false,
  };
}

export function setPhoto(state, blob, name) {
  state.photo = blob;
  state.photoName = name || state.photoName;
  // A new photo invalidates everything downstream of it.
  state.pendingDraft = null;
  state.generationId = null;
  state.portraitBusy = false;
  state.characterDrafts = {};
  state.bookId = null;
  state.pages = [];
  state.bookStatus = null;
  state.phase = null;
  state.error = null;
}

export function clearPhoto(state) {
  setPhoto(state, null, "photo.jpg");
}

export function setDraft(state, generationId) {
  state.pendingDraft = generationId;
  state.error = null;
}

export function approveDraft(state) {
  state.generationId = state.pendingDraft;
  state.pendingDraft = null;
  // Approving a portrait starts a *fresh* book. A book id left over from an
  // earlier failed attempt would otherwise make isApproved() false and tell
  // the user to "make a portrait first" even though one is right there.
  state.bookId = null;
  state.bookStatus = null;
  state.pages = [];
  state.phase = null;
  state.error = null;
}

export function beginPortrait(state) {
  state.portraitBusy = true;
}

export function endPortrait(state) {
  state.portraitBusy = false;
}

export function setCharacterDraft(state, key, generationId, cartoonUrl) {
  state.characterDrafts[key] = { generationId, cartoonUrl };
  state.error = null;
}

export function clearCharacterDraft(state, key) {
  delete state.characterDrafts[key];
}

export function characterDraft(state, key) {
  return state.characterDrafts[key] || null;
}

export function setBook(state, bookId) {
  state.bookId = bookId;
  state.bookStatus = "pending";
  state.phase = "pages";
  state.pages = [];
  state.error = null;
}

export function applyStatus(state, payload) {
  state.bookStatus = payload.status;
  // The job's phase tells "the last picture is drawn" from "the PDF exists":
  // without it the reader sees a full progress bar and assumes the book is
  // ready while the pages are still being assembled.
  state.phase = payload.phase || null;
  state.pages = payload.pages || [];
  state.error = payload.error || null;
}

export function isApproved(state) {
  return Boolean(state.generationId) && !state.bookId;
}

export function isBookInProgress(state) {
  return (
    Boolean(state.bookId) &&
    state.bookStatus !== "complete" &&
    state.bookStatus !== "failed"
  );
}

export function isBookFinished(state) {
  return state.bookStatus === "complete" && Boolean(state.bookId);
}

export function isBookFailed(state) {
  return state.bookStatus === "failed";
}

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
    /* The running book job. */
    bookId: null,
    pages: [],
    bookStatus: null,
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
  state.bookId = null;
  state.pages = [];
  state.bookStatus = null;
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
}

export function retryPortrait(state) {
  state.pendingDraft = null;
}

export function setBook(state, bookId) {
  state.bookId = bookId;
  state.bookStatus = "pending";
  state.pages = [];
  state.error = null;
}

export function applyStatus(state, payload) {
  state.bookStatus = payload.status;
  state.pages = payload.pages || [];
  state.error = payload.error || null;
}

export function isApproved(state) {
  return Boolean(state.generationId) && !state.bookId;
}

export function isBookFinished(state) {
  return state.bookStatus === "complete" && Boolean(state.bookId);
}

export function isBookFailed(state) {
  return state.bookStatus === "failed";
}

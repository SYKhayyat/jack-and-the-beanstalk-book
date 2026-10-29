/* Book view — every DOM read or write for the book page lives here.
   Reads the model; is never read by it; never calls fetch. */

export const elements = {
  form: () => document.getElementById("book-form"),
  photo: () => document.getElementById("photo"),
  useCamera: () => document.getElementById("use-camera"),
  cameraPane: () => document.getElementById("camera-pane"),
  cameraVideo: () => document.getElementById("camera-video"),
  cameraShutter: () => document.getElementById("camera-shutter"),
  cameraCancel: () => document.getElementById("camera-cancel"),
  cameraMessage: () => document.getElementById("camera-message"),
  photoMessage: () => document.getElementById("photo-message"),
  preview: () => document.getElementById("preview"),
  previewWrap: () => document.getElementById("preview-wrap"),

  makePortrait: () => document.getElementById("make-portrait"),

  reviewStage: () => document.getElementById("review-stage"),
  reviewPortrait: () => document.getElementById("review-portrait"),
  approve: () => document.getElementById("approve-portrait"),
  reject: () => document.getElementById("reject-portrait"),
  rejectBody: () => document.getElementById("reject-body"),
  rejectChips: () => document.getElementById("reject-chips"),
  rejectRetry: () => document.getElementById("reject-retry"),
  reviewMessage: () => document.getElementById("review-message"),

  bookStage: () => document.getElementById("book-stage"),
  bookProgress: () => document.getElementById("book-progress"),
  progressTrack: () => document.getElementById("book-progress-track"),
  progressBar: () => document.getElementById("book-progress-bar"),
  pageGrid: () => document.getElementById("page-grid"),
  pdfPanel: () => document.getElementById("book-pdf-panel"),
  pdfFrame: () => document.getElementById("book-pdf-frame"),
  pdfLink: () => document.getElementById("book-pdf-link"),
  restart: () => document.getElementById("book-restart"),

  promptPanel: () => document.getElementById("prompt-panel"),
  promptCartoon: () => document.getElementById("prompt-cartoon"),
  promptJsonLink: () => document.getElementById("prompt-json-link"),
};

/* The optional character-photo inputs, one per photoable cast slot. Empty
   inputs are ignored by the controller; a slot never exists for the cow. */
export function characterInputs() {
  return Array.from(document.querySelectorAll("input[data-character]"));
}

export function showPhotoMessage(message, kind = "info") {
  const node = elements.photoMessage();
  if (!message) {
    node.classList.add("hidden");
    return;
  }
  node.textContent = message;
  node.classList.toggle("error", kind === "error");
  node.classList.remove("hidden");
}

/* Track the current blob URL so a re-pick never leaks the previous blob. */
let previewUrl = null;

export function renderPhotoPreview(blob) {
  const preview = elements.preview();
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(blob);
  preview.src = previewUrl;
  elements.previewWrap().classList.remove("hidden");
}

export function clearPhotoPreview() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  const preview = elements.preview();
  preview.removeAttribute("src");
  elements.previewWrap().classList.add("hidden");
}

export function showCameraPane(open) {
  elements.cameraPane().classList.toggle("hidden", !open);
}

export function showCameraMessage(message) {
  const node = elements.cameraMessage();
  if (!message) {
    node.classList.add("hidden");
    return;
  }
  node.textContent = message;
  node.classList.remove("hidden");
}

export function oneStepChecked() {
  return Boolean(document.getElementById("one_step")?.checked);
}

export function showRestart() {
  elements.restart().classList.remove("hidden");
}

/* Transparency: the wording the model actually received. The cartoon prompt
   text arrives with the cartoonify response; the page-by-page JSON becomes
   linkable once the book exists. */
export function showPrompts({ cartoonPrompt, bookId }) {
  if (cartoonPrompt) {
    elements.promptCartoon().textContent = cartoonPrompt;
  }
  if (bookId) {
    const link = elements.promptJsonLink();
    link.href = `/book-media/${bookId}/prompts.json`;
    link.classList.remove("hidden");
  }
  elements.promptPanel().classList.remove("hidden");
}

export function setBusyPortrait(message) {
  const button = elements.makePortrait();
  button.disabled = Boolean(message);
  button.textContent = message || "Make the cartoon portrait";
}

export function showReview(portraitUrl, message) {
  elements.reviewStage().classList.remove("hidden");
  elements.reviewPortrait().src = portraitUrl;
  showReviewMessage(message);
  elements.approve().disabled = false;
  elements.reject().disabled = false;
}

export function showReviewMessage(message) {
  const node = elements.reviewMessage();
  if (!message) {
    node.classList.add("hidden");
    return;
  }
  node.textContent = message;
  node.classList.remove("hidden");
}

export function hideReview() {
  elements.reviewStage().classList.add("hidden");
}

/* The review stage's feedback flow: "Not right" opens the chip panel, the
   retry inside it re-runs the portrait with the ticked reasons as corrections.
   The chips are a closed set the server validates, so the controller only ever
   ships their values. */
export function showRejectBody(open) {
  elements.rejectBody().classList.toggle("hidden", !open);
  elements.reject().setAttribute("aria-expanded", String(open));
}

export function rejectBodyOpen() {
  return !elements.rejectBody().classList.contains("hidden");
}

export function rejectChipValues() {
  const chips = elements.rejectChips().querySelectorAll('input[name="reason"]:checked');
  return Array.from(chips, (chip) => chip.value);
}

export function resetRejectChips() {
  for (const chip of elements.rejectChips().querySelectorAll('input[name="reason"]')) {
    chip.checked = false;
  }
}

export function showBookStage() {
  elements.bookStage().classList.remove("hidden");
}

export function showBookError(message) {
  elements.bookProgress().textContent = message;
  elements.bookProgress().classList.add("error");
}

export function hideBookError() {
  elements.bookProgress().classList.remove("error");
}

export function renderBookProgress(done, total) {
  const node = elements.bookProgress();
  node.classList.remove("error");
  node.textContent =
    done >= total
      ? "Assembling the pages into a PDF..."
      : `${done} of ${total} pages made \u2014 check back in a moment`;
  const track = elements.progressTrack();
  track.setAttribute("aria-valuemax", String(total));
  track.setAttribute("aria-valuenow", String(done));
  elements.progressBar().style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
}

/* One tile per page: waiting, being drawn now, or finished with its picture.
   Rebuilt from the status payload on every poll, so a page's tile changes the
   moment the server says so. */
export function renderBookPages(pages) {
  elements.pageGrid().replaceChildren(...pages.map(buildPageTile));
}

function buildPageTile(page) {
  const state =
    page.status === "done" ? "done" : page.status === "making" ? "making" : "pending";
  const tile = document.createElement("figure");
  tile.className = `page-tile ${state}`;

  const label = document.createElement("figcaption");
  label.textContent = `Page ${page.number}`;
  tile.append(label);

  if (state === "done" && page.url) {
    const image = document.createElement("img");
    image.src = page.url;
    image.alt = page.title ? `Page ${page.number}: ${page.title}` : `Page ${page.number}`;
    image.loading = "lazy";
    tile.append(image);
  } else {
    const status = document.createElement("span");
    status.className = "page-tile-state";
    status.textContent = state === "making" ? "Drawing..." : "Waiting";
    tile.append(status);
  }
  return tile;
}

export function showPdfViewer(pdfUrl) {
  /* The finished book, in one big box: the PDF itself, not page pictures. */
  elements.pdfFrame().src = pdfUrl;
  elements.pdfPanel().classList.remove("hidden");
}

export function renderBookFinished(bookId, pdfUrl) {
  showPdfViewer(pdfUrl);
  const link = elements.pdfLink();
  link.href = pdfUrl;
  link.download = `book-${bookId}.pdf`;
  link.classList.remove("hidden");
  elements.restart().classList.remove("hidden");
  elements.bookProgress().textContent = "Your book is ready.";
  elements.bookProgress().classList.remove("error");
  const approve = elements.approve();
  approve.disabled = true;
  approve.textContent = "Book finished";
}

export function resetBookStage() {
  elements.pdfFrame().removeAttribute("src");
  elements.pdfPanel().classList.add("hidden");
  elements.pdfLink().classList.add("hidden");
  elements.restart().classList.add("hidden");
  elements.pageGrid().replaceChildren();
  elements.progressBar().style.width = "0%";
  elements.progressTrack().setAttribute("aria-valuenow", "0");
  hideBookError();
  elements.bookProgress().textContent = "";
}

/* Read the advanced settings + who-the-star fields as one FormData fragment,
   without the photo or the free-text fields (the controller adds those). */
export function readCharacterSettings() {
  const fields = [
    "aspect_ratio",
    "apparent_age",
    "skin_tone",
    "hair_style",
    "hair_color",
    "outfit",
    "expression",
    "likeness_strength",
    "photo_gender",
  ];
  const settings = new FormData();
  for (const name of fields) {
    const node = document.getElementById(name);
    if (node && node.value) settings.append(name, node.value);
  }
  return settings;
}

export function readAboutMe() {
  const byId = (id) => document.getElementById(id);
  const name = byId("child-name");
  const aboutName = byId("about-name");
  const aboutAge = byId("about-age");
  const aboutTitle = byId("about-title");
  const details = byId("details");
  const about = {
    child_name: name.value.trim(),
    about_name: aboutName.value.trim(),
    about_age: aboutAge.value === "" ? null : aboutAge.value,
    about_title: aboutTitle.value.trim(),
    details: details.value,
  };
  if (!about.about_name) about.about_name = about.child_name;
  if (!about.child_name) about.child_name = about.about_name;
  return about;
}

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
  reviewMessage: () => document.getElementById("review-message"),

  bookStage: () => document.getElementById("book-stage"),
  bookProgress: () => document.getElementById("book-progress"),
  bookPages: () => document.getElementById("book-pages"),
  pdfLink: () => document.getElementById("book-pdf-link"),
  restart: () => document.getElementById("book-restart"),
};

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

export function setBusyPortrait(message) {
  const button = elements.makePortrait();
  button.disabled = Boolean(message);
  button.textContent = message || "Step 4 · Make the cartoon portrait";
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
      ? "Assembling the pages into a PDF…"
      : `Making page ${done + 1} of ${total}…\u2014 check back in a moment`;
}

export function renderBookPages(state) {
  const grid = elements.bookPages();
  grid.replaceChildren();
  for (const page of state.pages) {
    const figure = document.createElement("figure");
    figure.className = `book-page book-page-${page.status}`;
    const number = document.createElement("span");
    number.className = "book-page-number";
    number.textContent = `Page ${page.number}`;
    const image = document.createElement("img");
    image.alt = `Page ${page.number}`;
    if (page.url) {
      image.src = page.url;
    } else {
      image.classList.add("placeholder");
      image.title = "Not generated yet";
    }
    figure.append(number, image);
    grid.append(figure);
  }
}

export function renderBookFinished(bookId, pdfUrl) {
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
  elements.pdfLink().classList.add("hidden");
  elements.restart().classList.add("hidden");
  elements.bookPages().replaceChildren();
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
  const name = document.getElementById("child-name");
  const age = document.getElementById("child-age");
  const gender = document.getElementById("child-gender");
  const details = document.getElementById("details");
  return {
    child_name: name.value.trim(),
    child_age: age.value === "" ? null : age.value,
    child_gender: gender.value.trim(),
    details: details.value,
  };
}

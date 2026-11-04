/* One book's page: view the finished PDF, redraw a page or a face, edit the
   About copy, delete the book. All model calls happen through the book's own
   edit endpoint, then the page polls the ordinary status endpoint. */

import { createCameraSource } from "./photo.js";
import { preparePhoto } from "./downscale.js";
import { createBookDetailClient } from "./api.js";

const bookId = document.body.dataset.bookId;
const MAX_UPLOAD = Number(document.body.dataset.maxUploadMb || 10) * 1024 * 1024;
const POLL_MS = 1500;

const camera = createCameraSource(document.getElementById("edit-camera-video"));
const api = createBookDetailClient(bookId);
let capturedFace = null;
let previewUrl = null;
let polling = false;

document.getElementById("edit-page-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void redrawPage();
});
document.getElementById("edit-face-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void replaceFace();
});
document.getElementById("redraw-cover").addEventListener("click", () => void redrawCover());
// Only a failed book renders this button: it draws just the pages (and the
// cover) that never finished, and keeps every finished page as it is.
document.getElementById("finish-book")?.addEventListener("click", (event) => {
  event.currentTarget.disabled = true; // one click, one job: a second POST is a 409
  void finishBook();
});
document.getElementById("edit-about-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void saveAbout();
});
document.getElementById("delete-book").addEventListener("click", () => void deleteBook());
for (const button of document.querySelectorAll("[data-delete-book]")) {
  button.addEventListener("click", () => void deleteLibraryBook(button));
}
for (const button of document.querySelectorAll(".edit-page-button")) {
  button.addEventListener("click", () => {
    document.getElementById("edit-page-number").value = button.dataset.pageNumber;
    document.getElementById("edit-page-form").scrollIntoView({ behavior: "smooth" });
  });
}
wireCamera();
wireReader();

// A page opened mid-render (a reload, a second tab, a trip through sign-in)
// must pick the progress up itself; otherwise it sits on its first message.
if (!["complete", "failed"].includes(document.body.dataset.bookStatus)) {
  startPolling();
}

async function postEdit(fields) {
  try {
    return await api.edit(fields);
  } catch (error) {
    if (error.status === 401) {
      // The session went away. Sign in and come straight back to this book.
      window.location.assign(`/login?next=/books/${bookId}`);
      return null;
    }
    throw error;
  }
}

async function redrawPage() {
  const form = new FormData();
  form.append("action", "page");
  form.append("page_number", document.getElementById("edit-page-number").value);
  for (const chip of document.querySelectorAll('#edit-chips input[name="reason"]:checked')) {
    form.append("reason", chip.value);
  }
  await runEdit(() => postEdit(form), "Redrawing that page…");
}

async function finishBook() {
  const form = new FormData();
  form.append("action", "retry");
  await runEdit(() => postEdit(form), "Finishing the pages that are missing…");
}

async function redrawCover() {
  const form = new FormData();
  form.append("action", "cover");
  await runEdit(() => postEdit(form), "Redrawing the cover…");
}

async function replaceFace() {
  const key = document.getElementById("edit-character").value;
  const file = document.getElementById("edit-face-photo").files?.[0];
  const blob = capturedFace || file;
  if (!blob) {
    showMessage("Choose a photo first.", true);
    return;
  }
  try {
    showMessage("Preparing the new portrait…");
    const prepared = await preparePhoto(blob, { maxBytes: MAX_UPLOAD });
    const portraitForm = new FormData();
    portraitForm.append("photo", prepared.blob, blob.name || "face.jpg");
    const portrait = await api.cartoonify(portraitForm);
    const form = new FormData();
    form.append("action", "character");
    form.append("character_key", key);
    form.append("generation_id", portrait.generation_id);
    await runEdit(() => postEdit(form), "Replacing the face and redrawing its pages…");
  } catch (error) {
    showMessage(error.message || "That photo could not be used.", true);
  }
}

async function saveAbout() {
  const form = new FormData();
  form.append("action", "about");
  for (const id of ["child-name", "about-name", "about-age", "about-title", "details"]) {
    const node = document.getElementById(id);
    if (node && node.value !== "") form.append(node.name, node.value);
  }
  try {
    showMessage("Rebuilding the PDF…");
    await postEdit(form);
    window.location.reload();
  } catch (error) {
    showMessage(error.message || "The About page could not be saved.", true);
  }
}

async function deleteBook() {
  if (!window.confirm("Delete this book and all of its pictures?")) return;
  try {
    const deleted = await api.remove();
    if (deleted) {
      window.location.assign("/");
      return;
    }
    showMessage("This book is already gone.", true);
  } catch (error) {
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    showMessage(error.message || "The book could not be deleted.", true);
  }
}

/* The sidebar is the fastest way out of a book: one click on its own × , no
   need to open the book first. Deleting the book this page is showing leaves
   nowhere to stand, so those go home. */
async function deleteLibraryBook(button) {
  const otherId = button.dataset.deleteBook;
  const title = button.dataset.deleteTitle || "this book";
  if (!window.confirm(`Delete "${title}" and all of its pictures?`)) return;
  button.disabled = true;
  try {
    const deleted = await api.removeOther(otherId);
    if (!deleted) {
      button.closest(".library-item")?.remove();
      return;
    }
    if (otherId === bookId) {
      window.location.assign("/");
      return;
    }
    button.closest(".library-item")?.remove();
  } catch (error) {
    button.disabled = false;
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    showMessage(error.message || "That book could not be deleted.", true);
  }
}

async function runEdit(start, message) {
  try {
    showMessage(message);
    const result = await start();
    if (!result) return;
    startPolling();
  } catch (error) {
    showMessage(error.message || "The edit could not start.", true);
    enableFinish();
  }
}

function enableFinish() {
  const finish = document.getElementById("finish-book");
  if (finish) finish.disabled = false;
}

function startPolling() {
  if (polling) return;
  polling = true;
  poll();
}

async function poll() {
  try {
    const payload = await api.status();
    if (payload.status === "complete") {
      window.location.reload();
      return;
    }
    if (payload.status === "failed") {
      polling = false;
      showMessage(payload.error || "The book stopped early.", true);
      enableFinish();
      return;
    }
    const done = (payload.pages || []).filter((page) => page.status === "done").length;
    const total = (payload.pages || []).length;
    if (payload.phase === "assembling") {
      showMessage("All the pages are made — putting the book together…");
    } else {
      showMessage(`${done} of ${total} pages ready — still working…`);
    }
    window.setTimeout(poll, POLL_MS);
  } catch (error) {
    polling = false;
    if (error.status === 401) {
      // The session went away mid-edit. Sign in and come straight back.
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    showMessage(error.message || "The book cannot be reached right now.", true);
  }
}

function showMessage(message, isError = false) {
  // The status line at the top is where the reader is looking; the line under
  // the edit forms repeats it for whoever just pressed a button down there.
  for (const id of ["book-status-line", "edit-message"]) {
    const node = document.getElementById(id);
    if (!node) continue;
    node.textContent = message;
    node.classList.toggle("error", isError);
    node.classList.remove("hidden");
  }
}

/* --- the reader ------------------------------------------------------------ */

/* The finished book is read in the page: the picture and its story line.
   The PDF is still one click away, but nothing here depends on it. */
function wireReader() {
  const reader = document.getElementById("book-reader");
  if (!reader) return;
  const pages = Array.from(reader.querySelectorAll(".reader-page"));
  const label = document.getElementById("reader-label");
  const prev = document.getElementById("reader-prev");
  const next = document.getElementById("reader-next");
  if (!pages.length) return;
  let index = 0;

  const show = (wanted) => {
    index = Math.max(0, Math.min(pages.length - 1, wanted));
    pages.forEach((page, position) => {
      page.classList.toggle("current", position === index);
      page.classList.toggle("hidden", position !== index);
    });
    label.textContent = `Page ${index + 1} of ${pages.length}`;
    prev.disabled = index === 0;
    next.disabled = index === pages.length - 1;
  };

  prev.addEventListener("click", () => show(index - 1));
  next.addEventListener("click", () => show(index + 1));
  show(0);
}

/* --- camera ---------------------------------------------------------------- */

function wireCamera() {
  document.getElementById("edit-use-camera").addEventListener("click", async () => {
    try {
      await camera.open();
      document.getElementById("edit-camera-pane").classList.remove("hidden");
      cameraMessage("");
    } catch (error) {
      cameraMessage(cameraOpenMessage(error));
    }
  });
  document.getElementById("edit-camera-shutter").addEventListener("click", () => {
    camera
      .capture()
      .then((frame) => {
        camera.close();
        document.getElementById("edit-camera-pane").classList.add("hidden");
        capturedFace = frame;
        renderFacePreview(frame);
      })
      .catch((error) => cameraMessage(error.message));
  });
  document.getElementById("edit-camera-cancel").addEventListener("click", () => {
    camera.close();
    document.getElementById("edit-camera-pane").classList.add("hidden");
  });
}

function renderFacePreview(blob) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(blob);
  document.getElementById("edit-face-preview").src = previewUrl;
  document.getElementById("edit-face-preview-wrap").classList.remove("hidden");
}

function cameraMessage(message) {
  const node = document.getElementById("edit-camera-message");
  if (!message) {
    node.classList.add("hidden");
    return;
  }
  node.textContent = message;
  node.classList.remove("hidden");
}

function cameraOpenMessage(error) {
  if (error.name === "NotAllowedError") {
    return "The browser blocked the camera — allow it in the site settings, or choose a file.";
  }
  if (error.name === "NotFoundError") {
    return "No camera found on this device — choose a file instead.";
  }
  return "The camera could not open — the file picker still works.";
}

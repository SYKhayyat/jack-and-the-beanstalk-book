/* The book stage: one spread at a time, drawn live, then kept and edited.

   The server's status document is the only source of truth: every number on
   screen comes from GET /api/book/{id}, and a reload rebuilds everything from
   it. The page you are on lives in the URL (#cover, #page-7, #about), so a
   reload or a second tab lands on the same spread. */

import { createBookClient } from "./api.js";
import { createCameraSource, isTouchDevice } from "./photo.js";
import { preparePhoto } from "./downscale.js";

const POLL_MS = 1500;
const MAX_QUIET_POLLS = 4;
const RESTING_AFTER_POLLS = 8; // ~12s without a new picture reads as "frozen"

const body = document.body;
const bookId = body.dataset.bookId;
const bookTitle = body.dataset.bookTitle || "this book";
const MAX_UPLOAD = Number(body.dataset.maxUploadMb || 10) * 1024 * 1024;
const api = createBookClient(bookId);

const slot = document.getElementById("book-slot");
const spreads = Array.from(slot.querySelectorAll(".spread"));
const mapTiles = Array.from(document.querySelectorAll(".map-tile"));
const lastIndex = spreads.length - 1;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

let index = 0;
let status = body.dataset.bookStatus;
let polling = false;
let awaitingEdit = false;
let lastDone = -1;
let quietPolls = 0;

show(initialIndex(), { animate: false });
wireTurning();
wireEdits();
if (!["complete", "failed"].includes(status)) startPolling();

/* --- turning pages ----------------------------------------------------------- */

function spreadFromHash() {
  const hash = window.location.hash.replace("#", "");
  if (hash === "cover") return 0;
  if (hash === "about") return lastIndex;
  const match = /^page-(\d+)$/.exec(hash);
  if (match) {
    const wanted = Number(match[1]);
    if (wanted >= 1 && wanted < lastIndex) return wanted;
  }
  return null;
}

/* With no page in the URL: the About page of a finished book, otherwise the
   last picture that has arrived (the cover while nothing has). */
function initialIndex() {
  const fromHash = spreadFromHash();
  if (fromHash !== null) return fromHash;
  if (status === "complete") return lastIndex;
  let last = 0;
  spreads.forEach((spread, position) => {
    if (position < lastIndex && spread.dataset.status === "done") last = position;
  });
  return last;
}

function spreadName(position) {
  if (position === 0) return { hash: "cover", label: "Cover" };
  if (position === lastIndex) return { hash: "about", label: "About" };
  return { hash: `page-${position}`, label: `Page ${position} of ${lastIndex - 1}` };
}

function show(wanted, { animate = true } = {}) {
  const next = Math.max(0, Math.min(lastIndex, wanted));
  const direction = next >= index ? "forward" : "back";
  index = next;
  spreads.forEach((spread, position) => {
    spread.hidden = position !== index;
    spread.classList.remove("turn-forward", "turn-back");
  });
  if (animate && !reducedMotion) {
    const current = spreads[index];
    void current.offsetWidth; // restart the animation
    current.classList.add(direction === "forward" ? "turn-forward" : "turn-back");
  }
  const { hash, label } = spreadName(index);
  document.getElementById("page-label").textContent = label;
  history.replaceState(null, "", `#${hash}`);
  document.getElementById("turn-back").disabled = index === 0;
  document.getElementById("turn-next").disabled = index === lastIndex;
  mapTiles.forEach((tile, position) => tile.classList.toggle("current", position === index));
  // The cover and the About page have their own buttons in the strip, so
  // "Redraw this page" only appears on a story page.
  document.getElementById("edit-page").hidden = index === 0 || index === lastIndex;
}

function wireTurning() {
  document.getElementById("turn-back").addEventListener("click", () => show(index - 1));
  document.getElementById("turn-next").addEventListener("click", () => show(index + 1));
  slot.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    if (!swiped) show(index + 1);
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, dialog")) return;
    if (event.key === "ArrowRight") show(index + 1);
    else if (event.key === "ArrowLeft") show(index - 1);
  });
  for (const tile of mapTiles) {
    tile.addEventListener("click", () => show(Number(tile.dataset.goto)));
  }
  // A link to #page-7 on this same page (or the back button) moves the book.
  window.addEventListener("hashchange", () => {
    const wanted = spreadFromHash();
    if (wanted !== null && wanted !== index) show(wanted);
  });
  let startX = null;
  let swiped = false;
  slot.addEventListener("pointerdown", (event) => {
    startX = event.clientX;
    swiped = false;
  });
  slot.addEventListener("pointerup", (event) => {
    if (startX === null) return;
    const dx = event.clientX - startX;
    startX = null;
    if (Math.abs(dx) < 40) return;
    swiped = true;
    show(dx < 0 ? index + 1 : index - 1);
  });
}

/* --- live progress ------------------------------------------------------------- */

function startPolling() {
  if (polling) return;
  polling = true;
  poll(0);
}

function poll(failures) {
  window.setTimeout(async () => {
    try {
      const payload = await api.status();
      apply(payload, failures);
    } catch (error) {
      if (error.status === 401) {
        window.location.assign(`/login?next=/books/${bookId}`);
        return;
      }
      if (failures + 1 < MAX_QUIET_POLLS) {
        poll(failures + 1);
        return;
      }
      polling = false;
      caption("The book can't be reached right now — keep this tab open and reload in a moment.");
    }
  }, POLL_MS);
}

function apply(payload) {
  const state = payload.status;
  if (awaitingEdit && state === "complete") {
    // The edit may not have registered yet; wait for the job to show up.
    awaitingEdit = awaitingEdit > 1 ? awaitingEdit - 1 : false;
    if (awaitingEdit) {
      poll(0);
      return;
    }
  }
  if (state !== "complete") awaitingEdit = false;
  status = state;
  const pages = Array.isArray(payload.pages) ? payload.pages : [];
  let done = 0;
  for (const entry of pages) {
    const position = Number(entry.number);
    if (!(position >= 0 && position < lastIndex)) continue;
    if (entry.status === "done") done += 1;
    paint(position, entry);
  }
  const total = lastIndex;
  setProgress(done, total);

  if (state === "complete") {
    // The finished book has an About spread and downloads the server renders;
    // a reload is the honest way to show them. The hash keeps your page.
    window.location.reload();
    return;
  }
  if (state === "failed") {
    polling = false;
    caption(`${done} of ${total} pictures — ${payload.error || "this book stopped early."}`);
    document.getElementById("failed-row").classList.remove("hidden");
    document.getElementById("failed-message").textContent =
      payload.error || "Some pictures didn't make it.";
    document.getElementById("edit-strip").classList.remove("hidden");
    return;
  }
  if (payload.phase === "assembling") {
    caption("All the pictures are drawn — putting the book together…");
  } else {
    quietPolls = done > lastDone ? 0 : quietPolls + 1;
    lastDone = Math.max(lastDone, done);
    caption(
      quietPolls >= RESTING_AFTER_POLLS
        ? `${done} of ${total} pictures drawn. The pencil is resting for a moment — it picks up again on its own.`
        : `Your book is being drawn. ${done} of ${total} pictures drawn.`,
    );
  }
  poll(0);
}

function paint(position, entry) {
  const spread = spreads[position];
  const tile = mapTiles[position];
  const state = entry.status || "pending";
  if (tile) {
    tile.className = `map-tile ${state}${position === index ? " current" : ""}`;
    tile.setAttribute("aria-label", `${spreadName(position).label}, ${state}`);
    if (state === "done" && entry.url && !tile.querySelector("img")) {
      const thumb = document.createElement("img");
      thumb.src = entry.url;
      thumb.alt = "";
      tile.prepend(thumb);
    }
  }
  if (!spread || spread.dataset.status === state) return;
  spread.dataset.status = state;
  const art = spread.querySelector(".spread-art");
  if (state === "done" && entry.url) {
    const img = document.createElement("img");
    img.src = entry.url;
    img.alt = `${spreadName(position).label}: ${entry.title || ""}`;
    art.replaceChildren(img);
    if (position === index && !reducedMotion) art.classList.add("landed");
  } else {
    const text = art.querySelector('[data-role="waiting-text"]');
    if (text) text.textContent = state === "failed" ? "This one didn't make it." : "being drawn…";
  }
}

function setProgress(done, total) {
  const track = document.getElementById("progress-track");
  track.setAttribute("aria-valuenow", String(done));
  document.getElementById("progress-fill").style.width = `${(100 * done) / total}%`;
}

function caption(text) {
  document.getElementById("progress-caption").textContent = text;
}

/* --- editing ---------------------------------------------------------------------- */

function say(text, isError = false) {
  const node = document.getElementById("edit-message");
  node.textContent = text;
  node.classList.toggle("error", isError);
  node.classList.toggle("hidden", !text);
}

async function runEdit(form, text) {
  try {
    say(text);
    await api.edit(form);
    // Back to the drawing view: the finished block steps aside until the
    // edit is done, then the page reloads on the finished book again.
    document.getElementById("finished-block").classList.add("hidden");
    document.getElementById("failed-row").classList.add("hidden");
    document.getElementById("progress-block").classList.remove("hidden");
    caption(text);
    awaitingEdit = 6;
    polling = false;
    startPolling();
  } catch (error) {
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    say(error.message || "That change couldn't start — try again.", true);
  }
}

function edit(fields) {
  const form = new FormData();
  for (const [name, value] of Object.entries(fields)) form.append(name, value);
  return form;
}

function wireEdits() {
  const redrawDialog = document.getElementById("redraw-dialog");
  const faceDialog = document.getElementById("face-dialog");
  const aboutDialog = document.getElementById("about-dialog");

  document.getElementById("edit-page").addEventListener("click", () => {
    if (index === 0 || index === lastIndex) return;
    document.getElementById("redraw-page-label").textContent = `page ${index}`;
    redrawDialog.showModal();
  });
  redrawDialog.addEventListener("close", () => {
    if (redrawDialog.returnValue !== "save") return;
    const form = edit({ action: "page", page_number: String(index) });
    for (const chip of redrawDialog.querySelectorAll('input[name="reason"]:checked')) {
      form.append("reason", chip.value);
      chip.checked = false;
    }
    void runEdit(form, `Redrawing page ${index}…`);
  });

  document.getElementById("edit-cover").addEventListener("click", () => {
    void runEdit(edit({ action: "cover" }), "Redrawing the cover…");
  });

  document.getElementById("finish-book").addEventListener("click", (event) => {
    event.currentTarget.disabled = true;
    void runEdit(edit({ action: "retry" }), "Finishing the pictures that are missing…");
  });

  document.getElementById("edit-about").addEventListener("click", () => aboutDialog.showModal());
  aboutDialog.addEventListener("close", () => {
    if (aboutDialog.returnValue !== "save") return;
    void saveAbout(new FormData(document.getElementById("about-edit-form")));
  });

  wireFaceDialog(faceDialog);

  document.getElementById("delete-book").addEventListener("click", () => void deleteBook());
}

async function saveAbout(fields) {
  const form = edit({ action: "about" });
  for (const [name, value] of fields) {
    if (String(value).trim() !== "") form.append(name, value);
  }
  try {
    say("Rewriting the About page…");
    await api.edit(form);
    window.location.hash = "about";
    window.location.reload();
  } catch (error) {
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    say(error.message || "The About page couldn't be saved.", true);
  }
}

function wireFaceDialog(dialog) {
  const camera = createCameraSource(document.getElementById("face-video"));
  const video = document.getElementById("face-video");
  const shutter = document.getElementById("face-shutter");
  const fileInput = document.getElementById("face-file");
  const previewFrame = document.getElementById("face-preview-frame");
  const preview = document.getElementById("face-preview");
  const note = document.getElementById("face-message");
  let photo = null;

  const setPhoto = (blob) => {
    photo = blob;
    if (preview.src) URL.revokeObjectURL(preview.src);
    preview.src = URL.createObjectURL(blob);
    previewFrame.classList.remove("hidden");
  };
  const tell = (text) => {
    note.textContent = text;
    note.classList.toggle("hidden", !text);
  };
  const stopCamera = () => {
    camera.close();
    video.classList.add("hidden");
    shutter.classList.add("hidden");
  };

  document.getElementById("edit-face").addEventListener("click", () => {
    photo = null;
    previewFrame.classList.add("hidden");
    tell("");
    dialog.showModal();
  });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    fileInput.value = "";
    if (file) setPhoto(file);
  });
  document.getElementById("face-camera").addEventListener("click", async () => {
    if (isTouchDevice()) {
      fileInput.setAttribute("capture", "user");
      fileInput.click();
      window.setTimeout(() => fileInput.removeAttribute("capture"), 0);
      return;
    }
    try {
      await camera.open();
      video.classList.remove("hidden");
      shutter.classList.remove("hidden");
      tell("");
    } catch {
      tell("The camera didn't open — uploading a photo still works.");
    }
  });
  shutter.addEventListener("click", () => {
    camera
      .capture()
      .then((frame) => {
        stopCamera();
        setPhoto(frame);
      })
      .catch((error) => tell(error.message));
  });
  dialog.addEventListener("close", () => {
    stopCamera();
    if (dialog.returnValue !== "save") return;
    const key = document.getElementById("face-character").value;
    if (!photo) {
      say("Choose a photo first, then replace the face.", true);
      return;
    }
    void replaceFace(key, photo);
  });
}

async function replaceFace(key, photo) {
  try {
    say("Drawing the new face…");
    const prepared = await preparePhoto(photo, { maxBytes: MAX_UPLOAD });
    const portraitForm = new FormData();
    portraitForm.append("photo", prepared.blob, "face.jpg");
    // The comic must be drawn for the chosen slot, or the edit refuses it.
    if (key !== "star") portraitForm.append("character", key);
    const portrait = await api.cartoonify(portraitForm);
    await runEdit(
      edit({ action: "character", character_key: key, generation_id: portrait.generation_id }),
      key === "star" ? "Redrawing every picture with the new face…" : "Redrawing their pages…",
    );
  } catch (error) {
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    say(error.message || "That photo couldn't be used.", true);
  }
}

async function deleteBook() {
  if (!window.confirm(`Delete "${bookTitle}" and every picture in it? This can't be undone.`)) return;
  try {
    await api.remove();
    window.location.assign("/");
  } catch (error) {
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    say(error.message || "The book couldn't be deleted.", true);
  }
}

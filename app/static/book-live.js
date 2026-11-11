/* The book stage: one spread at a time, drawn live, then kept and edited.

   The server's status document is the only source of truth: every number on
   screen comes from GET /api/book/{id}, and a reload rebuilds everything from
   it. The page you are on lives in the URL (#cover, #page-7, #about), so a
   reload or a second tab lands on the same spread. */

import { createBookClient } from "./api.js";
import { createCameraSource, isTouchDevice } from "./photo.js";
import { preparePhoto } from "./downscale.js";
import { createReaderLayout } from "./reader-layout.js";
import { createPageSound, createPageTurner } from "./page-turn.js";
import { createReadAloud, speechAvailable } from "./read-aloud.js";
import { celebrate } from "./celebrate.js";

const POLL_MS = 1500;
const MAX_QUIET_POLLS = 4;
const RESTING_AFTER_POLLS = 8; // ~12s without a new picture reads as "frozen"
const PRELOAD_AHEAD = 2; // pages either side fetched before they are turned to (P3)
const READ_PAUSE_MS = 900; // the breath between a page read aloud and the next

const body = document.body;
const bookId = body.dataset.bookId;
const bookTitle = body.dataset.bookTitle || "this book";
const bookSubtitle = body.dataset.bookSubtitle || "";
const bookStory = body.dataset.bookStory;
const bookGender = body.dataset.bookGender || "";
const MAX_UPLOAD = Number(body.dataset.maxUploadMb || 10) * 1024 * 1024;
const api = createBookClient(bookId);
const CELEBRATE_KEY = `book.celebrate.${bookId}`;

const reader = document.getElementById("reader");
const slot = document.getElementById("book-slot");
const spreads = Array.from(slot.querySelectorAll(".spread"));
const mapTiles = Array.from(document.querySelectorAll(".map-tile"));
const lastIndex = spreads.length - 1;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const MAX_ATTEMPTS = Number(body.dataset.maxAttempts) || 3;

const layout = createReaderLayout(reader, slot);
const turner = createPageTurner(slot, { reducedMotion });
const sound = createPageSound();
const voice = createReadAloud({ onPageRead: () => readOn() });
const records = spreads.map((spread) => attemptsFrom(spread.dataset));

let index = 0;
let status = body.dataset.bookStatus;
let polling = false;
let awaitingEdit = false;
let editing = false; // an edit is redrawing part of a book that was finished
let lastDone = -1;
let quietPolls = 0;

spreads.forEach((spread, position) => badgeAttempts(position, records[position]));
layout.fit();
show(initialIndex(), { animate: false });
wireTurning();
wireTools();
wireEdits();
welcomeFinishedBook();
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

/* With no page in the URL: a finished book opens on its cover, as a book does
   (U7); one being drawn opens on the newest picture that has arrived. */
function initialIndex() {
  const fromHash = spreadFromHash();
  if (fromHash !== null) return fromHash;
  if (status === "complete") return 0;
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
  if (next === index && !spreads[next].hidden) return;
  const previous = spreads[index];
  const direction = next >= index ? "forward" : "back";
  index = next;
  if (animate && previous !== spreads[index]) sound.play();
  void turner.turn(animate ? previous : null, spreads[index], direction);
  if (!animate) spreads.forEach((spread, position) => (spread.hidden = position !== index));
  const { hash, label } = spreadName(index);
  document.getElementById("page-label").textContent = label;
  history.replaceState(null, "", `#${hash}`);
  document.getElementById("turn-back").disabled = index === 0;
  document.getElementById("turn-next").disabled = index === lastIndex;
  mapTiles.forEach((tile, position) => tile.classList.toggle("current", position === index));
  // The cover and the About page have their own buttons in the strip, so
  // "Redraw this page" only appears on a story page.
  document.getElementById("edit-page").hidden = index === 0 || index === lastIndex;
  showAttempts(index);
  preloadAround(index);
  if (voice.reading) voice.readLeaf(spreads[index], bookTitle);
}

/* Hidden pages are lazy, so a page would only start downloading when turned
   to. Ask for the neighbours now, so the turn shows a picture, not a wait. */
function preloadAround(position) {
  for (let step = -PRELOAD_AHEAD; step <= PRELOAD_AHEAD; step += 1) {
    const spread = spreads[position + step];
    if (!spread) continue;
    for (const img of spread.querySelectorAll('img[loading="lazy"]')) img.loading = "eager";
  }
}

function wireTurning() {
  document.getElementById("turn-back").addEventListener("click", () => show(index - 1));
  document.getElementById("turn-next").addEventListener("click", () => show(index + 1));
  // Tap the left third to go back, anywhere else to go on, as with a book.
  slot.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    if (swiped) return;
    const box = slot.getBoundingClientRect();
    show(event.clientX - box.left < box.width / 3 ? index - 1 : index + 1);
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, dialog")) return;
    if (event.key === "ArrowRight" || event.key === "PageDown") show(index + 1);
    else if (event.key === "ArrowLeft" || event.key === "PageUp") show(index - 1);
    else if (event.key === "Home") show(0);
    else if (event.key === "End") show(lastIndex);
    else if (event.key === "Escape" && reader.classList.contains("story-time")) setStoryTime(false);
  });
  for (const tile of mapTiles) {
    tile.addEventListener("click", () => {
      show(Number(tile.dataset.goto));
      reader.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    });
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

/* --- reading tools: read to me, page sound, story time ------------------------- */

function setPressed(button, pressed, label) {
  button.setAttribute("aria-pressed", pressed ? "true" : "false");
  button.querySelector('[data-role="label"]').textContent = label;
}

function wireTools() {
  const readButton = document.getElementById("read-aloud");
  if (speechAvailable()) {
    readButton.hidden = false;
    readButton.addEventListener("click", () => {
      if (voice.reading) {
        voice.stop();
        setPressed(readButton, false, "Read to me");
        return;
      }
      voice.start();
      setPressed(readButton, true, "Stop reading");
      voice.readLeaf(spreads[index], bookTitle);
    });
  }

  const soundButton = document.getElementById("page-sound");
  setPressed(soundButton, sound.enabled, sound.enabled ? "Sound on" : "Sound off");
  soundButton.addEventListener("click", () => {
    sound.setEnabled(!sound.enabled);
    setPressed(soundButton, sound.enabled, sound.enabled ? "Sound on" : "Sound off");
    sound.play();
  });

  document.getElementById("story-time").addEventListener("click", () => {
    setStoryTime(!reader.classList.contains("story-time"));
  });
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && reader.classList.contains("story-time")) setStoryTime(false);
  });

  document.getElementById("read-book").addEventListener("click", () => {
    show(1);
    reader.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  });
}

/* Story time: the book fills the screen and the site steps away. Full screen
   where the browser allows it; a covering layer everywhere else (iPhone). */
function setStoryTime(on) {
  reader.classList.toggle("story-time", on);
  body.classList.toggle("in-story-time", on);
  setPressed(document.getElementById("story-time"), on, on ? "Leave story time" : "Story time");
  if (on && reader.requestFullscreen && !document.fullscreenElement) {
    reader.requestFullscreen().catch(() => {});
  } else if (!on && document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
  slot.focus({ preventScroll: true });
  layout.fit();
}

/* After a page is read aloud: a breath, then the next page; the About page
   is the end of the story, and the reading stops there. */
function readOn() {
  if (index >= lastIndex) {
    voice.stop();
    setPressed(document.getElementById("read-aloud"), false, "Read to me");
    return;
  }
  window.setTimeout(() => {
    if (voice.reading) show(index + 1);
  }, READ_PAUSE_MS);
}

/* The book has just finished drawing (the poll reloaded the page and left a
   note): open it big, on its cover, with confetti, once. */
function welcomeFinishedBook() {
  let fresh = false;
  try {
    fresh = window.sessionStorage.getItem(CELEBRATE_KEY) === "1";
    window.sessionStorage.removeItem(CELEBRATE_KEY);
  } catch {
    fresh = false;
  }
  if (!fresh || status !== "complete") return;
  window.scrollTo({ top: 0 });
  celebrate({ reducedMotion });
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
    // a reload is the honest way to show them. A book that has just been
    // drawn opens on its cover with a little celebration; an edit keeps your page.
    if (!editing) {
      try {
        window.sessionStorage.setItem(CELEBRATE_KEY, "1");
      } catch {
        // no storage: the book still opens, without the confetti
      }
      history.replaceState(null, "", "#cover");
    }
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
    caption("Step 2 of 2 · Putting the book together — every picture is drawn.");
  } else {
    quietPolls = done > lastDone ? 0 : quietPolls + 1;
    lastDone = Math.max(lastDone, done);
    // Say what is happening, not only how much is left. "45% for two minutes"
    // looks stuck; "checking page 7" looks like work.
    const stage = stageCaption(payload, done, total);
    caption(
      stage ||
        (quietPolls >= RESTING_AFTER_POLLS
          ? `${done} of ${total} pictures drawn. The pencil is resting for a moment — it picks up again on its own.`
          : `Your book is being drawn. ${done} of ${total} pictures drawn.`),
    );
  }
  poll(0);
}

/**
 * What the job is doing right now, as a sentence, or "" when there is nothing
 * specific to say. The pages are drawn in parallel, so the caption names the
 * step the whole book is on and how many pages are at each stage of it
 * (`stage_counts` from the server's status payload), rather than one page that
 * may already be finished by the time it is read.
 */
function stageCaption(payload, done, total) {
  const counts = payload.stage_counts || {};
  const drawing = Number(counts.painting) || 0;
  const checking = Number(counts.reviewing) || 0;
  const redrawing = Number(counts.retrying) || 0;
  const parts = [];
  if (drawing) parts.push(`${drawing} being drawn`);
  if (checking) parts.push(`${checking} being checked`);
  if (redrawing) parts.push(`${redrawing} being drawn again after a check`);
  if (!parts.length) return "";
  return `Step 1 of 2 · Drawing the pictures — ${done} of ${total} finished. Right now: ${parts.join(", ")}.`;
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
      thumb.src = entry.thumb || entry.url;
      thumb.alt = "";
      thumb.decoding = "async";
      tile.prepend(thumb);
    }
  }
  if (state === "done") {
    records[position] = attemptsFrom(entry);
    badgeAttempts(position, records[position]);
    if (position === index) showAttempts(index);
  }
  if (!spread || spread.dataset.status === state) return;
  spread.dataset.status = state;
  const art = spread.querySelector(".spread-art");
  if (state === "done" && entry.url) {
    art.replaceChildren(position === 0 ? coverFigure(entry) : pageImage(position, entry));
    layout.schedule();
    if (position === index && !reducedMotion) art.classList.add("landed");
  } else {
    const text = art.querySelector('[data-role="waiting-text"]');
    if (text) text.textContent = state === "failed" ? "This one didn't make it." : "being drawn…";
  }
}

function sizedImage(entry, alt) {
  const img = document.createElement("img");
  if (entry.srcset) {
    img.srcset = entry.srcset;
    img.sizes = getComputedStyle(reader).getPropertyValue("--page-w").trim() || "100vw";
  }
  img.src = entry.src || entry.url;
  img.alt = alt;
  img.decoding = "async";
  return img;
}

function pageImage(position, entry) {
  return sizedImage(entry, `${spreadName(position).label}: ${entry.title || ""}`);
}

/* The cover as the shelf and the PDF set it: the art with the title on it (U8). */
function coverFigure(entry) {
  const figure = document.createElement("figure");
  figure.className = "book-cover leaf-cover";
  figure.append(sizedImage(entry, `The cover: ${bookTitle}${bookSubtitle ? `, ${bookSubtitle}` : ""}`));
  const lockup = document.createElement("figcaption");
  lockup.className = "book-cover-lockup";
  lockup.setAttribute("aria-hidden", "true");
  const title = document.createElement("span");
  title.className = "book-cover-title";
  title.textContent = bookTitle;
  lockup.append(title);
  if (bookSubtitle) {
    const byline = document.createElement("span");
    byline.className = "book-cover-byline";
    byline.textContent = bookSubtitle;
    lockup.append(byline);
  }
  figure.append(lockup);
  return figure;
}

/* --- the attempt record -------------------------------------------------------
   Every picture goes through the quality check and is redrawn up to
   MAX_ATTEMPTS times; the badge says how many tries it took and which one is on
   the page, so the loop can be seen (and graded) rather than taken on trust. */

function attemptsFrom(source) {
  const attempts = Number(source.attempts) || 0;
  return {
    attempts,
    chosen: Number(source.chosenAttempt ?? source.chosen_attempt) || attempts,
    bestEffort: source.bestEffort === "true" || source.best_effort === true,
  };
}

function attemptWords({ attempts, chosen, bestEffort }) {
  if (bestEffort) {
    return `${attempts} of ${MAX_ATTEMPTS} tries — none passed every check, so the best one (try ${chosen}) is shown`;
  }
  if (attempts === 1) return `Passed its check on the first try (1 of ${MAX_ATTEMPTS})`;
  return `Passed its check on try ${chosen} of ${MAX_ATTEMPTS}`;
}

/* The tile in the page map carries every picture's record; the bar under the
   book carries the current page's. Never on the page itself: the page is the
   book, and the badge is about how it was made. */
function badgeAttempts(position, record) {
  if (!record.attempts) return;
  const words = attemptWords(record);
  const tile = mapTiles[position];
  if (tile) {
    let mark = tile.querySelector(".map-attempts");
    if (!mark) {
      mark = document.createElement("span");
      tile.append(mark);
    }
    mark.className = `map-attempts ${attemptTone(record)}`;
    mark.textContent = `${record.attempts}×`;
    mark.title = words;
    tile.setAttribute("aria-label", `${spreadName(position).label}, ${words}`);
  }
}

function attemptTone(record) {
  return record.bestEffort ? "best" : record.attempts > 1 ? "retried" : "first";
}

function showAttempts(position) {
  const chip = document.getElementById("attempt-chip");
  const record = records[position];
  if (!record || !record.attempts) {
    chip.className = "attempt-chip hidden";
    chip.textContent = "";
    return;
  }
  chip.className = `attempt-chip ${attemptTone(record)}`;
  chip.textContent = record.bestEffort
    ? `Best of ${record.attempts} tries`
    : `Try ${record.chosen} of ${MAX_ATTEMPTS}`;
  chip.title = attemptWords(record);
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
    editing = true;
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
    portraitForm.append("story", bookStory);
    // The portrait is drawn for the star's he or she, whoever's face it is.
    if (bookGender) portraitForm.append("photo_gender", bookGender);
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

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
let lastDrawn = -1;
let quietPolls = 0;
let shownFill = 0; // the bar's width only grows while a job runs

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
  // The cover is page 1, as the printed folio and the PDF count it; the
  // About spread is outside the numbering. So the first story page is page 2
  // of 21 (20 story pages + the cover), the same number printed on it (B7).
  return { hash: `page-${position}`, label: `Page ${position + 1} of ${lastIndex}` };
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
  // Each page still in flight, by number: its stage and which try it is on.
  const stages = state === "rendering" && payload.page_stages ? payload.page_stages : {};
  let done = 0;
  for (const entry of pages) {
    const position = Number(entry.number);
    if (!(position >= 0 && position < lastIndex)) continue;
    if (entry.status === "done") done += 1;
    paint(position, entry, stages[String(position)]);
  }
  const total = lastIndex;
  const inFlight = Object.values(stages);
  setProgress(done, total, inFlight);
  // Read to me, page sound and Story time need a page to act on. The cover
  // alone is not one, so the tools wait for the first finished story page
  // (U2); the server renders the same initial state.
  const readable =
    state === "complete" ||
    pages.some((entry) => Number(entry.number) >= 1 && entry.status === "done");
  document.getElementById("reader-tools")?.classList.toggle("hidden", !readable);

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
    const message = payload.error || "Some pictures didn't make it.";
    // Money is its own kind of stop: the wallet ran out, the pictures already
    // made were kept, and finishing draws the rest once there is money. Said
    // once in a notice that can be put away, not left over the page forever.
    const broke = payload.error_code === "out_of_funds";
    document.getElementById("failed-message").textContent = message;
    document.getElementById("failed-message").classList.toggle("hidden", broke);
    showFundsNotice(broke ? message : "");
    document.getElementById("edit-strip").classList.remove("hidden");
    return;
  }
  if (payload.phase === "assembling") {
    caption("Step 2 of 2 · Putting the book together — every picture is drawn.");
  } else {
    const drawn = Number(payload.pictures_drawn) || 0;
    quietPolls = done > lastDone || drawn > lastDrawn ? 0 : quietPolls + 1;
    lastDone = Math.max(lastDone, done);
    lastDrawn = Math.max(lastDrawn, drawn);
    // Say what is happening, not only how much is left. "45% for two minutes"
    // looks stuck; "4 being drawn, 2 being checked" looks like work. Resting is
    // said only when nothing at all is in flight: a page being drawn or checked
    // is work, however long since the last one was accepted.
    const stage = stageCaption(payload, inFlight, done, total);
    caption(
      stage ||
        (quietPolls >= RESTING_AFTER_POLLS
          ? `${done} of ${total} pages finished. The pencil is resting for a moment — it picks up again on its own.`
          : `Your book is being drawn. ${done} of ${total} pages finished.`),
    );
  }
  poll(0);
}

/**
 * What the job is doing right now, as a sentence, or "" when there is nothing
 * specific to say. The pages are drawn in parallel, so the caption names the
 * step the whole book is on and how many pages are at each stage of it, rather
 * than one page that may already be finished by the time it is read. Pictures
 * drawn run ahead of pages finished — the check turns some down and they are
 * drawn again — so both are said, and the count never sits still while work
 * goes on.
 */
function stageCaption(payload, inFlight, done, total) {
  const counts = inFlight.length ? countStages(inFlight) : payload.stage_counts || {};
  const drawing = Number(counts.painting) || 0;
  const checking = Number(counts.reviewing) || 0;
  const redrawing = Number(counts.retrying) || 0;
  const parts = [];
  if (drawing) parts.push(`${drawing} being drawn`);
  if (checking) parts.push(`${checking} being checked`);
  if (redrawing) parts.push(`${redrawing} being drawn again after a check`);
  if (!parts.length) return "";
  const drawn = Number(payload.pictures_drawn) || 0;
  const tally = drawn
    ? `${done} of ${total} pages finished, ${drawn} ${drawn === 1 ? "picture" : "pictures"} drawn so far`
    : `${done} of ${total} pages finished`;
  return `Step 1 of 2 · Drawing the pictures — ${tally}. Right now: ${parts.join(", ")}.`;
}

function countStages(inFlight) {
  const counts = {};
  for (const { stage } of inFlight) counts[stage] = (counts[stage] || 0) + 1;
  return counts;
}

/* How far along a page in flight is, as a share of one page: each try is a
   third of the way, and within a try the check comes after the drawing. */
function shareDone({ stage, attempt }) {
  const tries = Math.min(Math.max(Number(attempt) || 1, 1), MAX_ATTEMPTS);
  const within = stage === "reviewing" ? 0.7 : 0.3;
  return Math.min(0.9, (tries - 1 + within) / MAX_ATTEMPTS);
}

function tryWords({ stage, attempt }) {
  const tries = Number(attempt) || 1;
  const doing = stage === "reviewing" ? "being checked" : "being drawn";
  return `${doing}, try ${tries} of ${MAX_ATTEMPTS}`;
}

function paint(position, entry, inFlight) {
  const spread = spreads[position];
  const tile = mapTiles[position];
  const state = entry.status || "pending";
  const working = state !== "done" && inFlight;
  if (tile) {
    const look = working ? "making" : state;
    tile.className = `map-tile ${look}${position === index ? " current" : ""}`;
    tile.setAttribute(
      "aria-label",
      `${spreadName(position).label}, ${working ? tryWords(inFlight) : state}`,
    );
    markTry(tile, working ? inFlight : null);
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
  if (!spread) return;
  const art = spread.querySelector(".spread-art");
  if (spread.dataset.status !== state) {
    spread.dataset.status = state;
    if (state === "done" && entry.url) {
      art.replaceChildren(position === 0 ? coverFigure(entry) : pageImage(position, entry));
      layout.schedule();
      if (position === index && !reducedMotion) art.classList.add("landed");
      return;
    }
  }
  if (state === "done") return;
  const text = art.querySelector('[data-role="waiting-text"]');
  if (!text) return;
  if (state === "failed") text.textContent = "This one didn't make it.";
  else if (working) text.textContent = `${tryWords(inFlight)}…`;
  else text.textContent = status === "rendering" ? "waiting its turn…" : "being drawn…";
}

/* The try a page in flight is on, in the corner of its tile ("2/3"); gone once
   the page is finished, when the attempt badge takes the corner over. */
function markTry(tile, inFlight) {
  let mark = tile.querySelector(".map-try");
  if (!inFlight) {
    mark?.remove();
    return;
  }
  if (!mark) {
    mark = document.createElement("span");
    mark.className = "map-try";
    tile.append(mark);
  }
  mark.textContent = `${Number(inFlight.attempt) || 1}/${MAX_ATTEMPTS}`;
  mark.title = `Try ${Number(inFlight.attempt) || 1} of ${MAX_ATTEMPTS}`;
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

/* The bar counts pages finished, plus the share of each page in flight, so it
   moves while pictures are drawn and checked rather than only when one is
   accepted. It never runs backwards during a job (a page turned down by its
   check would otherwise pull it back), and it stripes while work is in flight.
   Screen readers are told the plain count of pages finished. */
function setProgress(done, total, inFlight = []) {
  const track = document.getElementById("progress-track");
  track.setAttribute("aria-valuenow", String(done));
  const partial = inFlight.reduce((sum, page) => sum + shareDone(page), 0);
  const fill = Math.min(1, (done + partial) / total);
  const rendering = status === "rendering";
  const width = rendering ? Math.max(shownFill, fill) : fill;
  // Forgotten once the job ends, so an edit started later starts from its own count.
  shownFill = rendering ? width : 0;
  track.classList.toggle("working", inFlight.length > 0);
  document.getElementById("progress-fill").style.width = `${100 * width}%`;
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

//: Set when the visitor puts the out-of-money notice away. The book is polled
//: every 1.5s, so a notice that came back on the next poll would be a nag.
let fundsNoticeDismissed = false;

/** Show (or take down) the "your wallet ran out" notice. */
function showFundsNotice(text) {
  const notice = document.getElementById("funds-notice");
  if (!notice) return;
  const wanted = Boolean(text) && !fundsNoticeDismissed;
  if (wanted) document.getElementById("funds-notice-text").textContent = text;
  notice.classList.toggle("hidden", !wanted);
}

function wireFundsNotice() {
  document.getElementById("funds-dismiss")?.addEventListener("click", () => {
    fundsNoticeDismissed = true;
    document.getElementById("funds-notice")?.classList.add("hidden");
  });
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

  // The same press, on a failed book and on a finished book that is missing a
  // picture: both finish only the pages that were never drawn (B2).
  for (const id of ["finish-book", "finish-missing"]) {
    document.getElementById(id)?.addEventListener("click", (event) => {
      event.currentTarget.disabled = true;
      void runEdit(edit({ action: "retry" }), "Finishing the pictures that are missing…");
    });
  }

  document.getElementById("edit-about").addEventListener("click", () => aboutDialog.showModal());
  aboutDialog.addEventListener("close", () => {
    if (aboutDialog.returnValue !== "save") return;
    void saveAbout(new FormData(document.getElementById("about-edit-form")));
  });

  wireFaceDialog(faceDialog);

  document.getElementById("delete-book").addEventListener("click", () => void deleteBook());
  wireFundsNotice();
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

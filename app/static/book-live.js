/* The book stage: one spread at a time, drawn live, then kept and edited.

   The server's status document is the only source of truth: every number on
   screen comes from GET /api/book/{id}, and a reload rebuilds everything from
   it. The page you are on lives in the URL (#cover, #page-7, #about), so a
   reload or a second tab lands on the same spread. */

import { createBookClient } from "./api.js";
import { createCameraSource, isTouchDevice } from "./photo.js";
import { preparePhoto } from "./downscale.js";
import { createReaderLayout } from "./reader-layout.js";
import { createPageTurner } from "./page-turn.js";
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
// The spreads follow the PDF's pages (R1/R4): cover, copyright, the story,
// "The End", About. A picture's number is not its place in that list, so the
// pictures (the cover is 0, story page n is n) are looked up by number.
const kindOf = (position) => spreads[position]?.dataset.kind;
const isPicture = (position) => ["cover", "story"].includes(kindOf(position));
const positionOfPicture = new Map();
spreads.forEach((spread, position) => {
  if (isPicture(position)) positionOfPicture.set(Number(spread.dataset.number), position);
});
const pictureCount = positionOfPicture.size;
const storyPages = pictureCount - 1;
const pictureAt = (position) => (isPicture(position) ? Number(spreads[position].dataset.number) : null);
// What read-aloud says on a leaf with no story text: the title on the cover,
// "The End" on its own page, and nothing on the copyright page.
const FALLBACK_WORDS = { cover: bookTitle, end: "The End", copyright: "" };
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const MAX_ATTEMPTS = Number(body.dataset.maxAttempts) || 3;

const layout = createReaderLayout(reader, slot);
const turner = createPageTurner(slot, { reducedMotion });
const voice = createReadAloud({ onPageRead: () => readOn() });

let index = 0;
let status = body.dataset.bookStatus;
// Set the moment the visitor turns a page themselves: from then on the stage
// stays where they put it instead of following the pictures (Idea 9).
let userNavigated = false;
let polling = false;
let awaitingEdit = false;
let editing = false; // an edit is redrawing part of a book that was finished
let lastDone = -1;
let lastDrawn = -1;
let quietPolls = 0;
let shownFill = 0; // the bar's width only grows while a job runs
let finishedWhileReading = false; // the book finished during story time

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
  const matter = spreads.findIndex((spread) => spread.dataset.kind !== "story" && hashOfKind(spread.dataset.kind) === hash);
  if (matter >= 0) return matter;
  const match = /^page-(\d+)$/.exec(hash);
  if (match) {
    const wanted = Number(match[1]);
    if (wanted >= 1) return positionOfPicture.get(wanted) ?? null;
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
    if (isPicture(position) && spread.dataset.status === "done") last = position;
  });
  return last;
}

function hashOfKind(kind) {
  return { cover: "cover", copyright: "copyright", end: "the-end", about: "about" }[kind];
}

function spreadName(position) {
  const kind = kindOf(position);
  if (kind === "cover") return { hash: "cover", label: "Cover" };
  if (kind === "copyright") return { hash: "copyright", label: "Copyright" };
  if (kind === "end") return { hash: "the-end", label: "The End" };
  if (kind === "about") return { hash: "about", label: "About" };
  // Story page n is page n, as its folio and the PDF print it (P4).
  const number = pictureAt(position);
  return { hash: `page-${number}`, label: `Page ${number} of ${storyPages}` };
}

/* A wide book on an upright screen shows a story leaf one half at a time:
   the picture, then its words (reader-layout.js "single"). */
function setHalf(half) {
  reader.dataset.half = half;
}

/* One turn forward or back. In halves, a story leaf turns from its picture to
   its words before the next leaf, and back from a picture lands on the words
   of the leaf before. Everywhere else it is a whole leaf. */
function step(delta) {
  const halves = layout.halves();
  const story = kindOf(index) === "story";
  if (halves && story && delta > 0 && reader.dataset.half === "art") {
    setHalf("words");
    return;
  }
  if (halves && story && delta < 0 && reader.dataset.half === "words") {
    setHalf("art");
    return;
  }
  const target = Math.max(0, Math.min(lastIndex, index + delta));
  show(target, { half: delta < 0 && halves && kindOf(target) === "story" ? "words" : "art" });
}

function show(wanted, { animate = true, half = "art" } = {}) {
  const next = Math.max(0, Math.min(lastIndex, wanted));
  if (next === index && !spreads[next].hidden) return;
  setHalf(half);
  const previous = spreads[index];
  const direction = next >= index ? "forward" : "back";
  index = next;
  void turner.turn(animate ? previous : null, spreads[index], direction);
  if (!animate) spreads.forEach((spread, position) => (spread.hidden = position !== index));
  const { hash, label } = spreadName(index);
  document.getElementById("page-label").textContent = label;
  history.replaceState(null, "", `#${hash}`);
  document.getElementById("turn-back").disabled = index === 0;
  document.getElementById("turn-next").disabled = index === lastIndex;
  const picture = pictureAt(index);
  mapTiles.forEach((tile) => tile.classList.toggle("current", Number(tile.dataset.goto) === picture));
  // "Redraw this page" stays put; a page with no picture greys it out.
  for (const id of ["edit-page", "redraw-here"]) {
    const button = document.getElementById(id);
    button.disabled = !isPicture(index);
    button.title = isPicture(index) ? "" : "This page has no picture to redraw";
  }
  preloadAround(index);
  readHere();
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
  document.getElementById("turn-back").addEventListener("click", () => {
    userNavigated = true;
    step(-1);
  });
  document.getElementById("turn-next").addEventListener("click", () => {
    userNavigated = true;
    step(1);
  });
  // Tap the left third to go back, anywhere else to go on, as with a book.
  slot.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    if (swiped) return;
    const box = slot.getBoundingClientRect();
    userNavigated = true;
    step(event.clientX - box.left < box.width / 3 ? -1 : 1);
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, dialog")) return;
    if (["ArrowRight", "PageDown", "ArrowLeft", "PageUp", "Home", "End"].includes(event.key)) {
      userNavigated = true;
    }
    if (event.key === "ArrowRight" || event.key === "PageDown") step(1);
    else if (event.key === "ArrowLeft" || event.key === "PageUp") step(-1);
    else if (event.key === "Home") show(0);
    else if (event.key === "End") show(lastIndex);
    else if (event.key === "Escape" && reader.classList.contains("story-time")) setStoryTime(false);
  });
  for (const tile of mapTiles) {
    tile.addEventListener("click", () => {
      userNavigated = true;
      show(positionOfPicture.get(Number(tile.dataset.goto)) ?? 0);
      reader.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    });
  }
  // A link to #page-7 on this same page (or the back button) moves the book.
  window.addEventListener("hashchange", () => {
    const wanted = spreadFromHash();
    if (wanted !== null && wanted !== index) {
      userNavigated = true;
      show(wanted);
    }
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
    userNavigated = true;
    step(dx < 0 ? 1 : -1);
  });
}

/* The finished book has an About spread and downloads the server renders; a
   reload is the honest way to show them. A book just drawn opens on its
   cover; an edit keeps your page. */
function openFinishedBook() {
  if (!editing) history.replaceState(null, "", "#cover");
  window.location.reload();
}

/* --- reading tools: play/pause, story time ------------------------------------ */

/* A pressed tool is a running tool: the glyph and the word both say what the
   next press does. */
function setPressed(button, pressed, label) {
  button.setAttribute("aria-pressed", pressed ? "true" : "false");
  button.setAttribute("aria-label", label);
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
      setPressed(readButton, true, "Pause");
      readHere();
    });
  }

  document.getElementById("story-time").addEventListener("click", () => {
    setStoryTime(!reader.classList.contains("story-time"));
  });
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && reader.classList.contains("story-time")) setStoryTime(false);
  });

  document.getElementById("read-book").addEventListener("click", () => {
    show(positionOfPicture.get(1) ?? 1);
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
  if (!on && finishedWhileReading) openFinishedBook();
}

function readHere() {
  if (voice.reading) voice.readLeaf(spreads[index], FALLBACK_WORDS[kindOf(index)] ?? "");
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
    const number = Number(entry.number);
    if (!positionOfPicture.has(number)) continue;
    if (entry.status === "done") done += 1;
    paint(number, entry, stages[String(number)]);
  }
  followThePictures(pages, state);
  const total = pictureCount;
  const inFlight = Object.values(stages);
  setProgress(done, total, inFlight);
  // Read to me and Story time need a page to act on. The cover
  // alone is not one, so the tools wait for the first finished story page
  // (U2); the server renders the same initial state.
  const readable =
    state === "complete" ||
    pages.some((entry) => Number(entry.number) >= 1 && entry.status === "done");
  document.getElementById("reader-tools")?.classList.toggle("hidden", !readable);

  if (state === "complete") {
    // A book that has just been drawn opens with a little celebration.
    if (!editing) {
      try {
        window.sessionStorage.setItem(CELEBRATE_KEY, "1");
      } catch {
        // no storage: the book still opens, without the confetti
      }
    }
    polling = false;
    // A reload would throw a reader out of story time (todo 11-17 #3). They
    // are told, and the finished book opens when they leave it themselves.
    if (reader.classList.contains("story-time")) {
      finishedWhileReading = true;
      document.getElementById("ready-note").hidden = false;
      return;
    }
    openFinishedBook();
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
    document.getElementById("redraw-here").classList.remove("hidden");
    return;
  }
  if (payload.phase === "paused") {
    // Idea 10: the operator stopped drawing, briefly. Nothing is lost and no
    // page needs pressing; the poll keeps running and the book carries on.
    caption("Drawing is paused for a moment. Everything drawn so far is kept — this book carries on by itself.");
  } else if (payload.phase === "preparing") {
    // Audit 11-15 S4: the wait before the first page starts is work too.
    caption(withTimeLeft("Getting the pictures ready — the drawing starts in a moment.", payload));
  } else if (payload.phase === "assembling") {
    caption("Every picture is drawn — putting the book together.");
  } else {
    const drawn = Number(payload.pictures_drawn) || 0;
    quietPolls = done > lastDone || drawn > lastDrawn ? 0 : quietPolls + 1;
    lastDone = Math.max(lastDone, done);
    lastDrawn = Math.max(lastDrawn, drawn);
    // One number, the one a reader can see: pictures ready of pictures in the
    // book. Pictures drawn and then turned down by the check, and how many
    // are at which stage, made "0 finished, 5 drawn" — true and confusing.
    // Resting is said only when nothing at all is in flight: a page being
    // drawn or checked is work, however long since the last one was accepted.
    const tally = `${done} of ${total} pictures ready`;
    caption(
      withTimeLeft(
        isWorking(payload, inFlight) || quietPolls < RESTING_AFTER_POLLS
          ? `Drawing your book — ${tally}.`
          : `${tally}. The pencil is resting for a moment — it picks up again on its own.`,
        payload,
      ),
    );
  }
  poll(0);
}

/**
 * The caption with about how long the book has left (A9), when the server
 * gave an estimate: the median time of the last whole books, counted from
 * when this one started. Rounded so it does not tick like a stopwatch, and
 * never a negative number: a book running over says it is nearly there, and
 * one well past the estimate says nothing about time at all.
 */
function withTimeLeft(sentence, payload) {
  const expected = Number(payload.expected_seconds);
  const started = Number(payload.job_started);
  if (!(expected > 0) || !(started > 0)) return sentence;
  const left = expected - (Date.now() / 1000 - started);
  if (left < -60) return sentence;
  if (left <= 10) return `${sentence} Almost done.`;
  if (left < 60) return `${sentence} About ${Math.ceil(left / 10) * 10} seconds left.`;
  const minutes = Math.round(left / 60);
  return `${sentence} About ${minutes} ${minutes === 1 ? "minute" : "minutes"} left.`;
}

/** Whether any page is being drawn or checked right now. */
function isWorking(payload, inFlight) {
  if (inFlight.length) return true;
  return Object.values(payload.stage_counts || {}).some((count) => Number(count) > 0);
}

/* While the book is being drawn, follow it: once the page being looked at has
   its picture, the stage moves to the newest finished page, so the arriving
   pictures are seen rather than waited behind. The first page the visitor
   turns themselves ends the show — a reader is never moved off their page. */
function followThePictures(pages, state) {
  if (userNavigated || document.hidden || state !== "rendering") return;
  if (spreads[index].dataset.status !== "done") return;
  let newest = index;
  for (const entry of pages) {
    const position = positionOfPicture.get(Number(entry.number));
    if (entry.status === "done" && position > newest) newest = position;
  }
  if (newest > index) show(newest, { animate: !reducedMotion });
}

/* How far along a page in flight is, as a share of one page: each try is a
   third of the way, and within a try the check comes after the drawing. */
function shareDone({ stage, attempt }) {
  const tries = Math.min(Math.max(Number(attempt) || 1, 1), MAX_ATTEMPTS);
  const within = stage === "reviewing" ? 0.7 : 0.3;
  return Math.min(0.9, (tries - 1 + within) / MAX_ATTEMPTS);
}

/* What a page in flight is doing. A redraw is named the same as a first draw:
   the number of tries is the quality loop's business, not the reader's. */
function tryWords({ stage }) {
  return stage === "reviewing" ? "checking it over" : "drawing it";
}

function paint(number, entry, inFlight) {
  const position = positionOfPicture.get(number);
  const spread = spreads[position];
  const tile = mapTiles.find((candidate) => Number(candidate.dataset.goto) === number);
  const state = entry.status || "pending";
  const working = state !== "done" && inFlight;
  if (tile) {
    const look = working ? "making" : state;
    tile.className = `map-tile ${look}${position === index ? " current" : ""}`;
    tile.setAttribute(
      "aria-label",
      `${spreadName(position).label}, ${working ? tryWords(inFlight) : state}`,
    );
    if (state === "done" && entry.url && !tile.querySelector("img")) {
      const thumb = document.createElement("img");
      thumb.src = entry.thumb || entry.url;
      thumb.alt = "";
      thumb.decoding = "async";
      // The picture lands in its tile rather than appearing there, so the map
      // reads as the book filling up (Idea 9).
      if (!reducedMotion) thumb.classList.add("landed");
      tile.querySelector(".map-title")?.remove();
      tile.prepend(thumb);
    }
  }
  if (!spread) return;
  const art = spread.querySelector(".spread-art");
  if (spread.dataset.status !== state) {
    spread.dataset.status = state;
    if (state === "done" && entry.url) {
      art.replaceChildren(number === 0 ? coverFigure(entry) : pageImage(position, entry));
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
  const aboutDialog = document.getElementById("about-dialog");
  const photo = wirePhotoPicker();
  const newPhoto = document.getElementById("redraw-new-photo");
  const chips = document.getElementById("redraw-chips");
  const go = document.getElementById("redraw-go");
  const showPhotoPicker = (on) => {
    document.getElementById("redraw-photo").classList.toggle("hidden", !on);
    go.textContent = on ? go.dataset.photoWords : go.dataset.pageWords;
    if (!on) photo.reset();
  };
  newPhoto.addEventListener("change", () => showPhotoPicker(newPhoto.checked));

  // One button, wherever it is pressed: the reader bar (story time too) or
  // the edit strip. On the cover it redraws the cover; on a page with no
  // picture it says so instead of doing nothing (todo 11-17 #7).
  const openRedraw = () => {
    if (!isPicture(index)) {
      say("This page has no picture to redraw — turn to the cover or a story page.", true);
      return;
    }
    const cover = kindOf(index) === "cover";
    document.getElementById("redraw-page-label").textContent = cover ? "the cover" : `page ${pictureAt(index)}`;
    // The cover is drawn from its own prompt, which takes no page faults.
    for (const chip of chips.querySelectorAll('input[name="reason"]')) {
      chip.checked = false;
      chip.closest(".chip").hidden = cover;
    }
    document.getElementById("redraw-help").textContent = cover
      ? "The cover is drawn again from scratch; the story pages stay as they are."
      : "Tick what went wrong. Every tick is a tested instruction; the rest of the book stays as it is.";
    newPhoto.checked = false;
    showPhotoPicker(false);
    redrawDialog.showModal();
  };
  document.getElementById("edit-page").addEventListener("click", openRedraw);
  document.getElementById("redraw-here").addEventListener("click", openRedraw);

  redrawDialog.addEventListener("close", () => {
    const chosen = photo.take();
    if (redrawDialog.returnValue !== "save") return;
    if (newPhoto.checked) {
      if (!chosen) {
        say("Choose a photo first, then redraw.", true);
        return;
      }
      void replaceFace(document.getElementById("face-character").value, chosen);
      return;
    }
    if (kindOf(index) === "cover") {
      void runEdit(edit({ action: "cover" }), "Redrawing the cover…");
      return;
    }
    const picture = pictureAt(index);
    const form = edit({ action: "page", page_number: String(picture) });
    for (const chip of chips.querySelectorAll('input[name="reason"]:checked')) {
      form.append("reason", chip.value);
    }
    void runEdit(form, `Redrawing page ${picture}…`);
  });

  // The same press, on a failed book and on a finished book that is missing a
  // picture: both finish only the pages that were never drawn (B2).
  for (const id of ["finish-book", "finish-missing"]) {
    document.getElementById(id)?.addEventListener("click", (event) => {
      event.currentTarget.disabled = true;
      void runEdit(edit({ action: "retry" }), "Finishing the pictures that are missing…");
    });
  }

  document.getElementById("book-size")?.addEventListener("change", (event) => {
    void changeSize(event.currentTarget);
  });

  document.getElementById("edit-about").addEventListener("click", () => aboutDialog.showModal());
  aboutDialog.addEventListener("close", () => {
    if (aboutDialog.returnValue !== "save") return;
    void saveAbout(new FormData(document.getElementById("about-edit-form")));
  });

  document.getElementById("delete-book").addEventListener("click", () => void deleteBook());
  wireFundsNotice();
}

/* The book's size, changed while reading: the server sets the PDF again (no
   picture is redrawn) and the page reloads on the same leaf in the new shape. */
async function changeSize(select) {
  const previous = reader.dataset.format;
  select.disabled = true;
  try {
    say("Setting the book in its new size…");
    await api.edit(edit({ action: "format", book_format: select.value }));
    window.location.reload();
  } catch (error) {
    if (error.status === 401) {
      window.location.assign(`/login?next=/books/${bookId}`);
      return;
    }
    select.value = previous;
    select.disabled = false;
    say(error.message || "The size couldn't be changed — try again.", true);
  }
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

/* The photo picker inside the redraw dialog: camera or upload, one preview.
   `take()` hands over the chosen photo and puts the camera away; `reset()`
   forgets it. */
function wirePhotoPicker() {
  const video = document.getElementById("face-video");
  const camera = createCameraSource(video);
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

  return {
    take() {
      stopCamera();
      const chosen = photo;
      photo = null;
      previewFrame.classList.add("hidden");
      tell("");
      return chosen;
    },
    reset() {
      stopCamera();
      photo = null;
      previewFrame.classList.add("hidden");
      tell("");
    },
  };
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

// --- the share link (W3) -------------------------------------------------------

/* Copy, with the input as the fallback: a browser without the clipboard API (or
   a page not served over HTTPS) still lets someone select the text. Delegated
   from the document because the button only exists on the page that was asked
   for a link, and a page that never asked still loads this module. */
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copy || "");
  if (!field) return;
  field.select();
  try {
    await navigator.clipboard.writeText(field.value);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Press Ctrl+C";
  }
  setTimeout(() => {
    button.textContent = "Copy link";
  }, 2500);
});

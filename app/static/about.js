/* The About screen, the last step: the words for the last page, the picture
   it shows, and the one
   button that starts the book. The name was asked first, in the studio, and
   is read from the same draft. Every field is optional; the only thing that
   can stop the book is a missing star portrait. */

import { createStudioClient } from "./api.js";
import { chosenId, forgetDraft, loadDraft, saveDraft } from "./studio-draft.js";

const story = document.body.dataset.story;
const api = createStudioClient();
const draft = loadDraft(story);
const form = document.getElementById("about-form");
const button = document.getElementById("make-book");
const headlineName = document.getElementById("about-headline-name");
const NAMES = JSON.parse(document.body.dataset.names || "{}");
const message = document.getElementById("about-message");
const TEXT_FIELDS = ["about_age", "about_title", "dedication", "details"];
let starting = false;

restoreFields();
renderHeadline();
renderCastSummary();
void checkStar();

form.addEventListener("input", () => {
  rememberFields();
  renderHeadline();
});
form.addEventListener("change", rememberFields);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  void startBook();
});

function field(name) {
  return form.elements.namedItem(name);
}

function restoreFields() {
  const saved = draft.about || {};
  for (const name of TEXT_FIELDS) {
    if (saved[name] !== undefined) field(name).value = saved[name];
  }
  if (saved.details || saved.dedication) document.getElementById("about-more").open = true;
  for (const name of ["about_photo"]) {
    if (!saved[name]) continue;
    const radio = form.querySelector(`input[name="${name}"][value="${saved[name]}"]`);
    if (radio) radio.checked = true;
  }
}

function starName() {
  return (draft.about?.about_name || "").trim();
}

function rememberFields() {
  // The name belongs to the studio's first step; keep it as it is.
  const about = { about_name: draft.about?.about_name || "" };
  for (const name of TEXT_FIELDS) about[name] = field(name).value;
  for (const name of ["about_photo"]) {
    about[name] = form.querySelector(`input[name="${name}"]:checked`)?.value || "";
  }
  draft.about = about;
  saveDraft(story, draft);
}

function renderHeadline() {
  const gender = draft.sections.star?.photoGender;
  const name = starName() || (gender && NAMES[gender]) || "the star";
  headlineName.textContent = name;
  for (const node of form.querySelectorAll('[data-role="star-name"]')) node.textContent = name;
}

/* "Star: ready · Mother: drawn from the story" — what goes into the book. */
function renderCastSummary() {
  const node = document.getElementById("about-cast-summary");
  let cast = [];
  try {
    cast = JSON.parse(node.dataset.cast || "[]");
  } catch {
    return;
  }
  node.textContent = cast
    .map(([key, name]) => `${name}: ${chosenId(draft.sections[key]) ? "a real face" : "drawn from the story"}`)
    .join(" · ");
}

async function checkStar() {
  const starId = chosenId(draft.sections.star);
  const alive = starId ? await api.draftAlive(starId) : false;
  if (alive) return;
  document.getElementById("about-missing").classList.remove("hidden");
  button.disabled = true;
}

function say(text, isError = false) {
  message.textContent = text;
  message.classList.toggle("error", isError);
  message.classList.toggle("hidden", !text);
}

async function startBook() {
  if (starting) return;
  const starId = chosenId(draft.sections.star);
  if (!starId) {
    await checkStar();
    return;
  }
  starting = true;
  button.disabled = true;
  say("Starting your book…");
  const body = new FormData();
  body.append("story", story);
  body.append("generation_id", starId);
  const name = starName();
  // One name for the book: the story and the About page both use it.
  if (name) {
    body.append("child_name", name);
    body.append("about_name", name);
  }
  for (const key of ["about_age", "about_title", "dedication", "details"]) {
    const value = field(key).value.trim();
    if (value) body.append(key, value);
  }
  for (const key of ["about_photo"]) {
    const value = form.querySelector(`input[name="${key}"]:checked`)?.value;
    if (value) body.append(key, value);
  }
  for (const [key, section] of Object.entries(draft.sections)) {
    if (key !== "star" && chosenId(section)) body.append("character", `${key}:${chosenId(section)}`);
  }
  try {
    const payload = await api.startBook(body);
    // The faces now belong to the book. The studio starts fresh next time,
    // so the shelf never offers "Keep going" on a book already made.
    forgetDraft(story);
    window.location.assign(`/books/${payload.book_id}`);
  } catch (error) {
    starting = false;
    button.disabled = false;
    if (error.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      return;
    }
    if (error.status === 402) {
      say(`${error.message} `, true);
      const link = document.createElement("a");
      link.href = "/wallet";
      link.textContent = "Open the wallet";
      message.append(link);
      return;
    }
    if (error.status === 404) {
      say("A photo in this book expired. Go back to the faces and add it again.", true);
      return;
    }
    say(error.message || "The book couldn't start — try again.", true);
  }
}

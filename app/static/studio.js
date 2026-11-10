/* The studio: the first three steps of making a book, one decision each.

     #name     the star's name (with the cover it goes on) and boy or girl
     #photo    the star's photo and storybook portrait
     #friends  optional faces for the rest of the cast

   The step lives in the URL hash, so Back and a reload land where you were.
   #photo and #friends open once the star has a name and boy or girl; the
   portrait can be drawn in either order, but the book waits for it. Within a step,
   each character section is filled, drawn and folded on its own.

   State that must survive a refresh (chosen draft ids, settings, folds) goes
   through studio-draft.js into localStorage. Photos stay in memory for this
   visit only; after a reload a retry fetches the stored original back from
   its draft, so nobody has to upload twice. */

import { createStudioClient, draftImageUrl } from "./api.js";
import { createCameraSource, isTouchDevice } from "./photo.js";
import { preparePhoto, UnreadableImageError } from "./downscale.js";
import { forgetDraft, loadDraft, saveDraft } from "./studio-draft.js";

const story = document.body.dataset.story;
const MAX_UPLOAD = Number(document.body.dataset.maxUploadMb || 10) * 1024 * 1024;

const api = createStudioClient();
const draft = loadDraft(story);
const photos = new Map(); // section key -> prepared Blob, this visit only
const objectUrls = new Map();
const busy = new Set();
const expired = new Set();

const cameraDialog = document.getElementById("camera-dialog");
const camera = createCameraSource(document.getElementById("camera-video"));
let cameraTarget = null;

const advancedDialog = document.getElementById("advanced-dialog");
const advancedForm = document.getElementById("advanced-form");
const DEFAULT_SETTINGS = readSettingsForm();
let advancedTarget = null;

const sectionEls = new Map(
  Array.from(document.querySelectorAll(".character[data-section]"), (el) => [el.dataset.section, el]),
);

const STEPS = Array.from(document.querySelectorAll(".step[data-step]"), (el) => el.dataset.step);
const nameInput = document.getElementById("star-name");
const coverPreview = document.getElementById("cover-preview");
const NAMES = JSON.parse(coverPreview.dataset.names || "{}");

for (const [key, el] of sectionEls) {
  wireSection(key, el);
  void restoreSection(key, el);
}
wireNameStep();
wireSteps();
wireCamera();
wireAdvanced();
wireFooter();
renderFooter();
showStep(stepFromHash(), { initial: true });

/* --- small helpers --------------------------------------------------------- */

function part(el, role) {
  return el.querySelector(`[data-role="${role}"]`);
}

function action(el, name) {
  return el.querySelector(`[data-action="${name}"]`);
}

function sectionData(key) {
  draft.sections[key] = draft.sections[key] || {};
  return draft.sections[key];
}

function persist() {
  saveDraft(story, draft);
}

function label(key) {
  return sectionEls.get(key)?.dataset.label || key;
}

function showMessage(key, text, isError = false) {
  const node = part(sectionEls.get(key), "message");
  node.textContent = text;
  node.classList.toggle("error", isError);
  node.classList.toggle("hidden", !text);
}

function goSignIn() {
  window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
}

function friendlyError(error, fallback) {
  if (error.status === 429) return "The pencil needs a breather — try again in a minute.";
  if (error.status === 404) return "That photo expired — add it again.";
  return error.message || fallback;
}

/* --- one section ----------------------------------------------------------- */

function wireSection(key, el) {
  // The star has a step of its own and never folds; the cast sections do.
  el.querySelector(".character-toggle")?.addEventListener("click", () => {
    setCollapsed(key, !isCollapsed(el));
  });
  action(el, "camera").addEventListener("click", () => void openCamera(key));
  part(el, "file").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void acceptPhoto(key, file);
  });
  action(el, "comic").addEventListener("click", () => void makeComic(key));
  action(el, "retry").addEventListener("click", () => {
    const reasons = Array.from(
      el.querySelectorAll('.review input[name="reason"]:checked'),
      (input) => input.value,
    );
    if (!reasons.length) {
      showMessage(key, "Tick what was wrong first, so the next try knows what to fix.", true);
      return;
    }
    void makeComic(key, reasons);
  });
  action(el, "advanced").addEventListener("click", () => openAdvanced(key));
  action(el, "again").addEventListener("click", () => void makeComic(key));
  action(el, "love")?.addEventListener("click", () => {
    if (STEPS.includes("friends")) location.hash = "friends";
    else window.location.assign(document.getElementById("studio-next").href);
  });
}

/* --- step 1: name and boy/girl ---------------------------------------------- */

function starName() {
  return (draft.about?.about_name || "").trim();
}

/* What a blank name becomes: Jack or Jane, Red. */
function shownName() {
  const gender = starGender();
  return starName() || (gender ? NAMES[gender] : "") || "the star";
}

function wireNameStep() {
  nameInput.value = draft.about?.about_name || "";
  const saved = draft.sections.star?.photoGender;
  if (saved) {
    const radio = document.querySelector(`input[name="photo_gender-star"][value="${saved}"]`);
    if (radio) radio.checked = true;
  }
  nameInput.addEventListener("input", () => {
    draft.about = { ...(draft.about || {}), about_name: nameInput.value };
    persist();
    renderNameStep();
  });
  for (const radio of document.querySelectorAll('input[name="photo_gender-star"]')) {
    radio.addEventListener("change", () => {
      sectionData("star").photoGender = radio.value;
      persist();
      renderNameStep();
    });
  }
  renderNameStep();
}

function renderNameStep() {
  const gender = starGender();
  const name = shownName();
  const lockup = coverPreview.querySelector(".book-cover-lockup");
  const title = lockup.querySelector(".book-cover-title");
  const byline = lockup.querySelector(".book-cover-byline");
  const coverName = starName() || (gender ? NAMES[gender] : "") || "";
  if (coverName) {
    title.textContent = coverPreview.dataset.title.replaceAll("{name}", coverName);
    if (byline) byline.textContent = coverPreview.dataset.byline.replaceAll("{name}", coverName);
  }
  const help = document.getElementById("star-name-help");
  help.textContent =
    !starName() && gender
      ? `Leave it blank and the book calls them ${NAMES[gender]}.`
      : "It goes on the cover and through the story.";
  document.getElementById("name-next").disabled = !gender;
  for (const node of document.querySelectorAll('[data-role="star-name"]')) node.textContent = name;
  const love = action(sectionEls.get("star"), "love");
  if (love) {
    love.firstChild.textContent = `That's ${gender === "girl" ? "her" : gender === "boy" ? "him" : "them"}! `;
  }
}

/* --- steps ------------------------------------------------------------------ */

function stepFromHash() {
  const key = location.hash.replace("#", "");
  return STEPS.includes(key) ? key : null;
}

/* The furthest step the visitor may be on right now. */
function reachable(step) {
  if (step === "name") return true;
  if (!starGender()) return false;
  return true;
}

function firstUnfinished() {
  if (!starGender()) return "name";
  if (!draft.sections.star?.chosenId) return "photo";
  return STEPS.includes("friends") ? "friends" : "photo";
}

function showStep(wanted, { initial = false } = {}) {
  let step = wanted || (initial ? firstUnfinished() : "name");
  if (!reachable(step)) step = firstUnfinished();
  for (const el of document.querySelectorAll(".step[data-step]")) {
    el.classList.toggle("hidden", el.dataset.step !== step);
  }
  for (const item of document.querySelectorAll("#steps [data-step-key]")) {
    if (item.dataset.stepKey === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  }
  if (location.hash.replace("#", "") !== step) history.replaceState(null, "", `#${step}`);
  if (!initial) {
    window.scrollTo({ top: 0, behavior: "smooth" });
    document.querySelector(`.step[data-step="${step}"] h1`)?.focus({ preventScroll: true });
  }
}

function wireSteps() {
  window.addEventListener("hashchange", () => showStep(stepFromHash()));
  for (const button of document.querySelectorAll("[data-go]")) {
    button.addEventListener("click", () => {
      location.hash = button.dataset.go;
    });
  }
  for (const heading of document.querySelectorAll(".step h1")) heading.tabIndex = -1;
}

function isCollapsed(el) {
  return part(el, "body").classList.contains("hidden");
}

function setCollapsed(key, collapsed) {
  const el = sectionEls.get(key);
  const toggle = el.querySelector(".character-toggle");
  if (!toggle) return;
  part(el, "body").classList.toggle("hidden", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
  sectionData(key).collapsed = collapsed;
  persist();
}

async function restoreSection(key, el) {
  const data = draft.sections[key] || {};
  if (typeof data.collapsed === "boolean" && key !== "star") {
    part(el, "body").classList.toggle("hidden", data.collapsed);
    el.querySelector(".character-toggle").setAttribute("aria-expanded", String(!data.collapsed));
  }
  renderSummary(key);
  if (!data.chosenId) {
    renderSection(key);
    return;
  }
  // Show what we remember straight away, then make sure it still exists.
  renderSection(key);
  if (!(await api.draftAlive(data.chosenId))) {
    expired.add(key);
    for (const field of ["generationId", "chosenId", "candidateUrls", "chosenIndex"]) {
      delete data[field];
    }
    persist();
    renderSection(key);
    showMessage(key, "That photo expired — add it again.", true);
    setCollapsed(key, false);
  }
  renderFooter();
}

/* Everything visible in a section follows from its data, so every change
   ends in this one function. */
function renderSection(key) {
  const el = sectionEls.get(key);
  const data = draft.sections[key] || {};
  const required = el.dataset.required === "true";
  const photoUrl = objectUrls.get(key) || (data.generationId ? draftImageUrl(data.generationId, "photo") : null);
  const comicUrl = data.chosenId ? draftImageUrl(data.chosenId) : null;
  const hasPhoto = Boolean(photoUrl);

  part(el, "plates").classList.toggle("hidden", !hasPhoto);
  part(el, "upload-label").textContent = hasPhoto ? "Change photo" : "Upload a photo";
  action(el, "camera").textContent = hasPhoto ? "Retake" : "Take a photo";
  const emptyNote = part(el, "empty-note");
  if (emptyNote) emptyNote.classList.toggle("hidden", hasPhoto);
  if (photoUrl) part(el, "photo-img").src = photoUrl;

  const comicImg = part(el, "comic-img");
  comicImg.classList.toggle("hidden", !comicUrl);
  part(el, "comic-empty").classList.toggle("hidden", Boolean(comicUrl));
  if (comicUrl) comicImg.src = comicUrl;

  // Once a portrait exists the happy path leads ("That's her!"); drawing it
  // again, which costs another picture, folds away under "Not quite right?".
  const comicButton = action(el, "comic");
  comicButton.disabled = busy.has(key) || !hasPhoto;
  comicButton.textContent = busy.has(key) ? "Drawing…" : "Draw the portrait";
  comicButton.classList.toggle("hidden", Boolean(comicUrl) && !busy.has(key));
  const love = action(el, "love");
  if (love) love.classList.toggle("hidden", !comicUrl || busy.has(key));
  action(el, "retry").disabled = busy.has(key);
  action(el, "again").disabled = busy.has(key);
  const review = part(el, "review");
  review.classList.toggle("hidden", !comicUrl || busy.has(key));
  if (!comicUrl) review.open = false;
  part(el, "comic-frame").classList.toggle("is-drawing", busy.has(key));

  renderTakes(key);

  const thumb = part(el, "thumb");
  const thumbUrl = comicUrl || photoUrl;
  thumb.style.backgroundImage = thumbUrl ? `url("${thumbUrl}")` : "";
  thumb.classList.toggle("filled", Boolean(thumbUrl));

  const state = part(el, "state");
  let word = required ? "no photo" : "optional";
  if (expired.has(key) && !hasPhoto) word = "expired";
  else if (busy.has(key)) word = "drawing";
  else if (comicUrl) word = "ready";
  else if (hasPhoto) word = "photo added";
  state.textContent = word;
  state.dataset.state = word.replace(" ", "-");
  el.dataset.state = word.replace(" ", "-");
}

function renderTakes(key) {
  const el = sectionEls.get(key);
  const data = draft.sections[key] || {};
  const strip = part(el, "takes");
  const urls = data.candidateUrls || [];
  strip.replaceChildren();
  strip.classList.toggle("hidden", urls.length < 2 || !data.chosenId);
  if (urls.length < 2) return;
  urls.forEach((url, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "take";
    button.setAttribute("role", "radio");
    const chosen = (data.chosenIndex || 0) === index;
    button.setAttribute("aria-checked", String(chosen));
    button.setAttribute("aria-label", `Take ${index + 1}${chosen ? ", chosen" : ""}`);
    button.disabled = busy.has(key);
    const img = document.createElement("img");
    img.src = url;
    img.alt = "";
    button.append(img);
    button.addEventListener("click", () => void chooseTake(key, index));
    strip.append(button);
  });
}

function renderSummary(key) {
  const el = sectionEls.get(key);
  const settings = { ...DEFAULT_SETTINGS, ...(draft.sections[key]?.settings || {}) };
  const changed = Object.keys(DEFAULT_SETTINGS).filter((name) => settings[name] !== DEFAULT_SETTINGS[name]);
  part(el, "summary").textContent = changed.length
    ? `· ${changed.length} change${changed.length === 1 ? "" : "s"}`
    : "";
}

/* --- photos ---------------------------------------------------------------- */

async function acceptPhoto(key, blob) {
  try {
    showMessage(key, "Getting the photo ready…");
    const prepared = await preparePhoto(blob, { maxBytes: MAX_UPLOAD });
    photos.set(key, prepared.blob);
    if (objectUrls.has(key)) URL.revokeObjectURL(objectUrls.get(key));
    objectUrls.set(key, URL.createObjectURL(prepared.blob));
    // One photo per section: a new photo replaces the comic made from the old.
    const data = sectionData(key);
    for (const field of ["generationId", "chosenId", "candidateUrls", "chosenIndex"]) {
      delete data[field];
    }
    expired.delete(key);
    persist();
    showMessage(key, prepared.scaled ? "Photo shrunk to fit. Now draw the portrait." : "Now draw the portrait.");
    renderSection(key);
    renderFooter();
  } catch (error) {
    showMessage(
      key,
      error instanceof UnreadableImageError
        ? "That file isn't a photo we can read — try a JPEG or PNG."
        : "That photo couldn't be prepared — try another.",
      true,
    );
  }
}

async function openCamera(key) {
  // On phones the system camera is the better camera: open the picker in
  // capture mode instead of an in-page video.
  if (isTouchDevice()) {
    const input = part(sectionEls.get(key), "file");
    input.setAttribute("capture", "user");
    input.click();
    window.setTimeout(() => input.removeAttribute("capture"), 0);
    return;
  }
  try {
    await camera.open();
    cameraTarget = key;
    document.getElementById("camera-title").textContent = `A photo of ${label(key).toLowerCase()}`;
    setCameraMessage("");
    cameraDialog.showModal();
  } catch (error) {
    showMessage(key, cameraOpenMessage(error), true);
  }
}

function wireCamera() {
  document.getElementById("camera-shutter").addEventListener("click", () => {
    const key = cameraTarget;
    camera
      .capture()
      .then((frame) => {
        cameraDialog.close();
        return acceptPhoto(key, frame);
      })
      .catch((error) => setCameraMessage(error.message));
  });
  document.getElementById("camera-cancel").addEventListener("click", () => cameraDialog.close());
  // However the dialog closes — Cancel, Escape, the shutter — the camera light goes off.
  cameraDialog.addEventListener("close", () => {
    camera.close();
    cameraTarget = null;
  });
}

function setCameraMessage(text) {
  const node = document.getElementById("camera-message");
  node.textContent = text;
  node.classList.toggle("hidden", !text);
}

function cameraOpenMessage(error) {
  if (error.name === "NotAllowedError") {
    return "The browser blocked the camera — allow it in the site settings, or upload a photo.";
  }
  if (error.name === "NotFoundError") return "No camera on this device — upload a photo instead.";
  if (error.name === "NotSupportedError") return "This browser has no camera — upload a photo instead.";
  return "The camera didn't open — uploading a photo still works.";
}

/* --- portraits ------------------------------------------------------------- */

function settingsBody(key) {
  const settings = { ...DEFAULT_SETTINGS, ...(draft.sections[key]?.settings || {}) };
  const body = new FormData();
  for (const [name, value] of Object.entries(settings)) body.append(name, String(value));
  body.append("photo_gender", starGender());
  return body;
}

/* The boy/girl answer from step 1, or null: there is deliberately no default. */
function starGender() {
  return draft.sections.star?.photoGender || null;
}

async function makeComic(key, reasons = []) {
  if (busy.has(key)) return;
  if (!starGender()) {
    location.hash = "name";
    return;
  }
  const data = sectionData(key);
  busy.add(key);
  renderSection(key);
  showMessage(key, reasons.length ? "Drawing it again, with those fixes…" : "Drawing the portrait. This takes a little while…");
  try {
    let photo = photos.get(key);
    if (!photo && data.generationId) photo = await api.draftPhoto(data.generationId);
    if (!photo) throw new Error("Add a photo first.");
    const body = settingsBody(key);
    body.append("photo", photo, `${key}.jpg`);
    // A portrait belongs to one story: its cast slot and the star's costume.
    body.append("story", story);
    if (key !== "star") body.append("character", key);
    for (const reason of reasons) body.append("reason", reason);
    const payload = await api.cartoonify(body);
    data.generationId = payload.generation_id;
    data.chosenId = payload.generation_id;
    data.chosenIndex = 0;
    data.candidateUrls = payload.images?.candidate_urls || [];
    expired.delete(key);
    persist();
    for (const chip of sectionEls.get(key).querySelectorAll('.review input[name="reason"]')) {
      chip.checked = false;
    }
    showMessage(
      key,
      data.candidateUrls.length > 1 ? "Pick the take you like best." : "",
    );
  } catch (error) {
    if (error.status === 401) {
      goSignIn();
      return;
    }
    showMessage(key, friendlyError(error, "The portrait didn't come out. Try again."), true);
  } finally {
    busy.delete(key);
    renderSection(key);
    renderFooter();
  }
}

async function chooseTake(key, index) {
  const data = sectionData(key);
  if (!data.generationId || busy.has(key) || (data.chosenIndex || 0) === index) return;
  busy.add(key);
  renderSection(key);
  try {
    data.chosenId =
      index === 0 ? data.generationId : (await api.choose(data.generationId, index)).generation_id;
    data.chosenIndex = index;
    persist();
    showMessage(key, `Take ${index + 1} it is.`);
  } catch (error) {
    if (error.status === 401) {
      goSignIn();
      return;
    }
    showMessage(key, friendlyError(error, "That take couldn't be chosen — try again."), true);
  } finally {
    busy.delete(key);
    renderSection(key);
    renderFooter();
  }
}

/* --- the look (Advanced) ---------------------------------------------------- */

function readSettingsForm() {
  const settings = {};
  for (const field of advancedForm.querySelectorAll("select, input[type=checkbox]")) {
    settings[field.name] = field.type === "checkbox" ? (field.checked ? "true" : "false") : field.value;
  }
  return settings;
}

function fillSettingsForm(settings) {
  for (const field of advancedForm.querySelectorAll("select, input[type=checkbox]")) {
    const value = settings[field.name];
    if (value === undefined) continue;
    if (field.type === "checkbox") field.checked = value === "true";
    else field.value = value;
  }
}

function openAdvanced(key) {
  advancedTarget = key;
  const name = label(key);
  document.getElementById("advanced-title").textContent =
    key === "star" ? `${shownName()}'s look` : `${name}'s look`;
  fillSettingsForm({ ...DEFAULT_SETTINGS, ...(draft.sections[key]?.settings || {}) });
  advancedDialog.showModal();
}

function wireAdvanced() {
  advancedDialog.addEventListener("close", () => {
    const key = advancedTarget;
    advancedTarget = null;
    if (!key || advancedDialog.returnValue !== "save") return;
    sectionData(key).settings = readSettingsForm();
    persist();
    renderSummary(key);
    showMessage(key, draft.sections[key].chosenId ? "Saved. Draw it again to see the new look." : "Saved.");
  });
}

/* --- the footer -------------------------------------------------------------- */

function wireFooter() {
  document.getElementById("studio-next").addEventListener("click", (event) => {
    if (event.currentTarget.getAttribute("aria-disabled") !== "true") return;
    event.preventDefault();
    location.hash = "photo";
    showMessage("star", "The book needs the star's portrait first: add a photo and draw it.", true);
  });
  document.getElementById("studio-start-over").addEventListener("click", () => {
    if (!window.confirm("Start over? This forgets every photo and portrait in this studio.")) return;
    forgetDraft(story);
    window.location.assign(window.location.pathname);
  });
}

function renderFooter() {
  const ready = [];
  const skipped = [];
  for (const [key, el] of sectionEls) {
    if (draft.sections[key]?.chosenId) ready.push(key);
    else if (el.dataset.required !== "true") skipped.push(key);
  }
  const starReady = Boolean(draft.sections.star?.chosenId) && !busy.has("star");
  const next = document.getElementById("studio-next");
  next.setAttribute("aria-disabled", String(!starReady));
  const summary = document.getElementById("studio-summary");
  if (!starReady) {
    summary.textContent = "";
    return;
  }
  const portraits = `${ready.length} portrait${ready.length === 1 ? "" : "s"} ready`;
  summary.textContent = skipped.length
    ? `${portraits} · ${skipped.length} optional character${skipped.length === 1 ? "" : "s"} drawn from the story`
    : portraits;
}

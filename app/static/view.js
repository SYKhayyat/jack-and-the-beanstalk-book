/* View — every DOM read and write on the page. Reads data handed to it
   (model snapshots, API payloads); never talks to the network, and nothing
   outside this module mutates the page. Elements are collected once here and
   re-exported so the controller can wire listeners without scanning the DOM. */

const byId = (id) => document.getElementById(id);

export const elements = {
  form: byId("generate-form"),
  submit: byId("submit"),
  photoInput: byId("photo"),
  previewWrap: byId("preview-wrap"),
  preview: byId("preview"),
  originalThumb: byId("original-thumb"),
  photoMessage: byId("photo-message"),
  useCamera: byId("use-camera"),
  cameraPane: byId("camera-pane"),
  cameraVideo: byId("camera-video"),
  cameraShutter: byId("camera-shutter"),
  cameraCancel: byId("camera-cancel"),
  cameraMessage: byId("camera-message"),
  oneStep: byId("one_step"),
  intensity: byId("cartoon_intensity"),
  intensityOutput: byId("intensity-output"),
  characterGender: byId("character_gender"),
  photoGenderControl: byId("photo-gender-control"),
  approveButton: byId("approve-cartoon"),
  rejectToggle: byId("reject-toggle"),
  rejectBody: byId("reject-body"),
  rejectButton: byId("reject-cartoon"),
  otherSceneButton: byId("other-scene"),
  cartoonStage: byId("cartoon-stage"),
  cartoonStatus: byId("cartoon-status"),
  cartoonCost: byId("cartoon-cost"),
  cartoonImage: byId("cartoon-image"),
  cartoonPrompt: byId("cartoon-prompt"),
  sceneStage: byId("scene-stage"),
  sceneStatus: byId("scene-status"),
  sceneCost: byId("scene-cost"),
  sceneTitle: byId("scene-title"),
  sceneImage: byId("scene-image"),
  sceneCartoonImage: byId("scene-cartoon-image"),
  sceneCaption: byId("scene-caption"),
  sceneCartoonPrompt: byId("scene-cartoon-prompt"),
  scenePrompt: byId("scene-prompt"),
  downloadScene: byId("download-scene"),
  candidatePicker: byId("candidate-picker"),
  candidateStrip: byId("candidate-strip"),
  filmstripBlock: byId("filmstrip-block"),
  filmstrip: byId("filmstrip"),
  lightbox: byId("lightbox"),
  lightboxImage: byId("lightbox-image"),
  lightboxCaption: byId("lightbox-caption"),
  lightboxClose: byId("lightbox-close"),
};

/* The one backend number the client needs: the upload-size backstop. */
export const pageLimits = {
  maxUploadBytes:
    Number(document.body?.dataset?.maxUploadMb || 10) * 1024 * 1024,
};

export function startOverButtons() {
  return Array.from(document.querySelectorAll("[data-start-over]"));
}

export function zoomButtons() {
  return Array.from(document.querySelectorAll(".zoom"));
}

/* --- photo ---------------------------------------------------------------- */

let previewUrl = null;

function releasePreviewUrl() {
  if (previewUrl) {
    URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  }
}

/* The preview must be the exact blob that gets uploaded, so callers hand in
   the prepared blob rather than the untouched original. */
export function renderPhotoPreview(blob) {
  releasePreviewUrl();
  previewUrl = URL.createObjectURL(blob);
  elements.preview.src = previewUrl;
  elements.originalThumb.src = previewUrl;
  elements.previewWrap.classList.remove("hidden");
}

export function clearPhotoPreview() {
  releasePreviewUrl();
  elements.preview.src = "";
  elements.originalThumb.src = "";
  elements.previewWrap.classList.add("hidden");
}

export function showPhotoMessage(message, kind = "info") {
  elements.photoMessage.textContent = message;
  elements.photoMessage.className = `inline-message${kind === "error" ? " error" : ""}`;
  elements.photoMessage.classList.toggle("hidden", !message);
}

/* --- camera pane ----------------------------------------------------------- */

export function showCameraPane(open) {
  elements.cameraPane.classList.toggle("hidden", !open);
  elements.useCamera.classList.toggle("hidden", open);
}

export function isCameraPaneOpen() {
  return !elements.cameraPane.classList.contains("hidden");
}

export function showCameraMessage(message) {
  elements.cameraMessage.textContent = message;
  elements.cameraMessage.classList.toggle("hidden", !message);
}

/* --- small controls -------------------------------------------------------- */

export function syncIntensityOutput() {
  elements.intensityOutput.textContent = elements.intensity.value;
}

export function syncSubmitLabel() {
  elements.submit.textContent = elements.oneStep.checked
    ? "Make both images"
    : "Step 1 · Cartoonify me";
}

/* The app cannot tell a boy from a girl in a photo, so when the user asks to
   "match the photo" we must ask which it is. */
export function syncPhotoGender() {
  elements.photoGenderControl.classList.toggle(
    "hidden",
    elements.characterGender.value !== "match",
  );
}

export function applyCaptureAttribute() {
  elements.photoInput.setAttribute("capture", "user");
}

export function toggleRejectBody() {
  /* Returns whether the chips section is now visible. */
  const nowHidden = elements.rejectBody.classList.toggle("hidden");
  elements.rejectToggle.setAttribute("aria-expanded", nowHidden ? "false" : "true");
  return !nowHidden;
}

/* --- form reads ------------------------------------------------------------ */

/* The photo and every setting except the scene, the one-step flag, and the
   rejection chips; the controller appends the prepared photo blob itself. */
export function readSettings() {
  const payload = new FormData(elements.form);
  ["photo", "scene", "one_step", "reason"].forEach((name) => payload.delete(name));
  return payload;
}

export function getSelectedReasons() {
  return Array.from(
    elements.form.querySelectorAll('#reject-chips input[name="reason"]:checked'),
  ).map((input) => input.value);
}

export function clearReasonChips() {
  elements.form
    .querySelectorAll('#reject-chips input[name="reason"]')
    .forEach((input) => {
      input.checked = false;
    });
}

export function getChosenScene() {
  return elements.form.querySelector('input[name="scene"]:checked').value;
}

/* Selects the not-currently-checked scene radio and returns its key. */
export function chooseOtherScene() {
  const other = elements.form.querySelector('input[name="scene"]:not(:checked)');
  if (!other) return null;
  other.checked = true;
  return other.value;
}

/* --- generation controls --------------------------------------------------- */

export function setGenerationControlsDisabled(disabled) {
  [
    elements.submit,
    elements.approveButton,
    elements.rejectToggle,
    elements.rejectButton,
    elements.otherSceneButton,
  ].forEach((button) => {
    button.disabled = disabled;
  });
}

export function setBusyCartoon(message) {
  setBusy(elements.cartoonStatus, elements.cartoonStage, message);
}

export function setBusyScene(message) {
  setBusy(elements.sceneStatus, elements.sceneStage, message);
}

export function showCartoonError(message) {
  setStatus(elements.cartoonStatus, message, "error");
  elements.cartoonStage.classList.remove("hidden");
}

export function showSceneError(message) {
  setStatus(elements.sceneStatus, message, "error");
  elements.sceneStage.classList.remove("hidden");
}

/* --- result rendering ------------------------------------------------------ */

function setBusy(statusEl, stageEl, message) {
  setStatus(statusEl, message, "busy");
  stageEl.classList.remove("hidden");
  stageEl.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setStatus(statusEl, message, kind) {
  statusEl.className = `status${kind ? ` ${kind}` : ""}`;
  statusEl.textContent = message;
}

export function renderCartoon(data, model, attemptNumber, onChoose) {
  elements.cartoonImage.src = model.candidateUrls[0];
  elements.cartoonPrompt.textContent = data.prompts.cartoon;
  elements.cartoonCost.textContent = `${model.calls} image call${model.calls > 1 ? "s" : ""} used`;

  renderCandidates(model.candidateUrls, data.settings.candidates, onChoose);
  renderFilmstrip(model.attempts);

  setStatus(
    elements.cartoonStatus,
    `Attempt ${attemptNumber} ready via ${data.provider}. ` +
      "Compare the two, then approve or tell me what went wrong.",
  );

  elements.sceneStage.classList.add("hidden");
  elements.cartoonStage.classList.remove("hidden");
  elements.cartoonStage.scrollIntoView({ behavior: "smooth", block: "start" });
}

export function renderScene(data, model, oneStepMode) {
  elements.sceneTitle.textContent = data.scene_title;
  elements.sceneImage.src = data.images.scene_url;
  elements.sceneCartoonImage.src = data.images.cartoon_url;
  elements.sceneCaption.textContent = data.caption;
  elements.sceneCartoonPrompt.textContent = data.prompts.cartoon;
  elements.scenePrompt.textContent = data.prompts.scene;
  elements.sceneCost.textContent = `${model.calls} image call${model.calls > 1 ? "s" : ""} used`;

  elements.downloadScene.href = data.images.scene_url;
  elements.downloadScene.download = `${data.scene_key}.${data.images.scene_extension || "png"}`;

  const warnings = data.warnings || [];
  setStatus(
    elements.sceneStatus,
    `Done via ${data.provider} (${data.model}). ` +
      `Generation ${data.generation_id.slice(0, 8)}.` +
      (warnings.length ? ` Note: ${warnings.join(" ")}` : ""),
  );

  elements.cartoonStage.classList.add("hidden");
  elements.sceneStage.classList.remove("hidden");
  elements.sceneStage.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderCandidates(urls, requested, onChoose) {
  if (urls.length < 2) {
    elements.candidatePicker.classList.add("hidden");
    elements.candidateStrip.innerHTML = "";
    return;
  }
  elements.candidatePicker.classList.remove("hidden");
  elements.candidateStrip.innerHTML = "";
  urls.forEach((url, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `candidate${index === 0 ? " chosen" : ""}`;
    button.innerHTML = `<img src="${url}" alt="Option ${index + 1}"><span>Option ${index + 1}</span>`;
    button.addEventListener("click", () => {
      elements.cartoonImage.src = url;
      elements.candidateStrip
        .querySelectorAll(".candidate")
        .forEach((node, i) => node.classList.toggle("chosen", i === index));
      onChoose(index);
    });
    elements.candidateStrip.appendChild(button);
  });
  if (requested > 1 && urls.length < requested) {
    const note = document.createElement("p");
    note.className = "field-help";
    note.textContent =
      `You asked for ${requested} options; the provider returned ${urls.length}.`;
    elements.candidateStrip.appendChild(note);
  }
}

function renderFilmstrip(attempts) {
  if (attempts.length < 2) {
    elements.filmstripBlock.classList.add("hidden");
    return;
  }
  elements.filmstripBlock.classList.remove("hidden");
  elements.filmstrip.innerHTML = "";
  attempts.forEach((attempt, index) => {
    const figure = document.createElement("figure");
    figure.innerHTML =
      `<img src="${attempt.url}" alt="Attempt ${index + 1}">` +
      `<figcaption>${index + 1} · ${attempt.style} · level ${attempt.intensity}</figcaption>`;
    figure.addEventListener("click", () =>
      openLightbox(attempt.url, `Attempt ${index + 1} · ${attempt.style}`));
    elements.filmstrip.appendChild(figure);
  });
}

/* --- lightbox -------------------------------------------------------------- */

export function openLightbox(src, caption) {
  if (!src) return;
  elements.lightboxImage.src = src;
  elements.lightboxCaption.textContent = caption;
  elements.lightbox.showModal();
}

/* --- reset ------------------------------------------------------------------ */

/* The visual half of "start over". The advanced <details> and any changed
   settings are deliberately untouched here: state should persist. */
export function resetStages() {
  elements.cartoonStage.classList.add("hidden");
  elements.sceneStage.classList.add("hidden");
  elements.filmstripBlock.classList.add("hidden");
  elements.candidatePicker.classList.add("hidden");
  elements.candidateStrip.innerHTML = "";
  setStatus(elements.cartoonStatus, "");
  setStatus(elements.sceneStatus, "");
}

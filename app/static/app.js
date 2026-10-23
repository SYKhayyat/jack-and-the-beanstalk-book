/* Controller — event wiring and orchestration only. No DOM reads or writes of
   its own (element references come from the view's exported map), no direct
   model clumps: every state change goes through the state module's functions.
   The API client and the photo sources are passed in, so fakes can replace
   any of them without touching another module. */

import {
  addAttempt,
  addCalls,
  beginRequest,
  chooseCandidate,
  clearPhoto,
  createState,
  endRequest,
  resetGeneration,
  setPhoto,
} from "./state.js";
import { createApiClient } from "./api.js";
import { preparePhoto, UnreadableImageError } from "./downscale.js";
import {
  createCameraSource,
  createFilePickerSource,
  isTouchDevice,
} from "./photo.js";
import {
  applyCaptureAttribute,
  clearPhotoPreview,
  clearReasonChips,
  chooseOtherScene,
  elements,
  getChosenScene,
  getSelectedReasons,
  openLightbox,
  pageLimits,
  readSettings,
  renderCartoon,
  renderPhotoPreview,
  renderScene,
  resetStages,
  setBusyCartoon,
  setBusyScene,
  setGenerationControlsDisabled,
  showCameraMessage,
  showCameraPane,
  isCameraPaneOpen,
  showCartoonError,
  showPhotoMessage,
  showSceneError,
  syncIntensityOutput,
  syncPhotoGender,
  syncSubmitLabel,
  toggleRejectBody,
  zoomButtons,
  startOverButtons,
} from "./view.js";

const model = createState();
const api = createApiClient();

const filePickerSource = createFilePickerSource(elements.photoInput);
const cameraSource = createCameraSource(elements.cameraVideo);

/* --- photo intake (the PhotoSource seam) ----------------------------------- */

/* Every source funnels through here: prepare (decode/validate/downscale),
   remember in the model, and render the exact blob that will be uploaded. */
async function acceptPhoto(sourceBlob, { fromCamera = false, sourceName = null } = {}) {
  showPhotoMessage("Checking and shrinking your photo…");
  try {
    const prepared = await preparePhoto(sourceBlob, {
      maxBytes: pageLimits.maxUploadBytes,
      force: fromCamera,
    });
    const name = sourceName || (prepared.scaled ? "photo.jpg" : (sourceBlob && sourceBlob.name) || "photo.jpg");
    setPhoto(model, prepared.blob, name);
    renderPhotoPreview(prepared.blob);
    showPhotoMessage("");
  } catch (error) {
    if (error instanceof UnreadableImageError) {
      showPhotoMessage(
        "That file could not be read as an image — try a JPEG, PNG or WEBP photo.",
        "error",
      );
    } else {
      showPhotoMessage(
        "Something went wrong while preparing that photo — try another one.",
        "error",
      );
    }
  }
}

elements.photoInput.addEventListener("change", () => {
  if (isCameraPaneOpen()) {
    closeCamera();
  }
  const file = filePickerSource.getBlob();
  if (!file) {
    clearPhoto(model);
    clearPhotoPreview();
    showPhotoMessage("");
    return;
  }
  acceptPhoto(file);
});

/* --- camera ------------------------------------------------------------------ */

function cameraErrorMessage(error) {
  const name = error && error.name;
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Camera permission was denied. Allow it in the browser, or choose a photo file instead.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No camera was found. Choose a photo file instead.";
  }
  if (name === "NotSupportedError") {
    return (
      "This browser cannot open the camera on this page (it needs HTTPS or " +
      "localhost). On a phone, the Choose-file button opens the camera directly."
    );
  }
  return "The camera could not be opened. Choose a photo file instead.";
}

async function openCamera() {
  showCameraMessage("");
  showCameraPane(true);
  try {
    await cameraSource.open();
  } catch (error) {
    closeCamera();
    showCameraMessage(cameraErrorMessage(error));
  }
}

/* Stops every track, so the camera indicator light genuinely goes off. */
function closeCamera() {
  cameraSource.close();
  showCameraPane(false);
}

async function captureFromCamera() {
  showCameraMessage("");
  try {
    const blob = await cameraSource.capture();
    closeCamera();
    await acceptPhoto(blob, { fromCamera: true, sourceName: "camera.jpg" });
  } catch (error) {
    showCameraMessage(
      `${error.message} Try once more, or choose a photo file instead.`,
      );
  }
}

elements.useCamera.addEventListener("click", openCamera);
elements.cameraShutter.addEventListener("click", captureFromCamera);
elements.cameraCancel.addEventListener("click", closeCamera);

if (isTouchDevice()) applyCaptureAttribute();

/* --- plan-level toggles ------------------------------------------------------ */

elements.intensity.addEventListener("input", syncIntensityOutput);
syncIntensityOutput();

elements.oneStep.addEventListener("change", syncSubmitLabel);
syncSubmitLabel();

elements.characterGender.addEventListener("change", syncPhotoGender);
syncPhotoGender();

/* --- submit and review actions ------------------------------------------------ */

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!hasPhoto()) return;
  if (elements.oneStep.checked) {
    runOneStep();
    return;
  }
  runCartoonify([]);
});

function hasPhoto() {
  if (model.photo && model.photo.blob) return true;
  showPhotoMessage("Please choose a photo first.", "error");
  return false;
}

/* Retry with the ticked reasons. With nothing ticked this is just another
   attempt at the same settings, which is why there is no separate retry button. */
elements.rejectButton.addEventListener("click", () => {
  if (!hasPhoto()) return;
  runCartoonify(getSelectedReasons());
});

/* "Not right" only reveals the chips — the choosing is the user's job. */
elements.rejectToggle.addEventListener("click", () => toggleRejectBody());

elements.approveButton.addEventListener("click", () => {
  if (!model.generationId) {
    showCartoonError("Cartoonify first, then approve.");
    return;
  }
  runCompose(getChosenScene());
});

elements.otherSceneButton.addEventListener("click", () => {
  const scene = chooseOtherScene();
  if (!scene) return;
  runCompose(scene);
});

[startOverButtons()].flat().forEach((button) => {
  button.addEventListener("click", () => {
    resetGeneration(model);
    clearReasonChips();
    resetStages();
    elements.form.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

/* Click either compare image to enlarge it. */
zoomButtons().forEach((button) => {
  button.addEventListener("click", () => {
    const which = button.dataset.zoom;
    openLightbox(
      which === "original" ? elements.originalThumb.src : model.candidateUrls[model.chosen],
      which === "original" ? "The photo you uploaded" : "Your cartoon",
    );
  });
});

elements.lightboxClose.addEventListener("click", () => elements.lightbox.close());
elements.lightbox.addEventListener("click", (event) => {
  if (event.target === elements.lightbox) elements.lightbox.close();
});

/* --- the two-step flow --------------------------------------------------------- */

async function runCartoonify(reasons) {
  if (model.requestInFlight) return;
  beginRequest(model);
  const attemptNumber = model.attempts.length + 1;
  setGenerationControlsDisabled(true);
  setBusyCartoon("Cartoonifying you…");

  try {
    const body = readSettings();
    body.append("photo", model.photo.blob, model.photo.name);
    reasons.forEach((reason) => body.append("reason", reason));

    const data = await api.cartoonify(body);
    model.generationId = data.generation_id;
    addCalls(model, data.billed_image_count || data.candidate_count || 1);
    model.candidateUrls = data.images.candidate_urls || [data.images.cartoon_url];
    chooseCandidate(model, 0);
    addAttempt(model, {
      url: model.candidateUrls[0],
      style: data.settings.cartoon_style,
      intensity: data.settings.cartoon_intensity,
    });
    renderCartoon(data, model, attemptNumber, (index) => chooseCandidate(model, index));
  } catch (error) {
    showCartoonError(error.message);
  } finally {
    endRequest(model);
    setGenerationControlsDisabled(false);
  }
}

async function runCompose(scene) {
  if (model.requestInFlight || !model.generationId) return;
  beginRequest(model);
  setGenerationControlsDisabled(true);
  setBusyScene(`Placing you in the scene: ${scene}…`);

  try {
    const body = new FormData();
    body.append("generation_id", model.generationId);
    body.append("scene", scene);
    body.append("candidate", String(model.chosen));

    const data = await api.compose(body);
    addCalls(model, 1);
    renderScene(data, model, false);
  } catch (error) {
    showSceneError(error.message);
  } finally {
    endRequest(model);
    setGenerationControlsDisabled(false);
  }
}

async function runOneStep() {
  if (model.requestInFlight) return;
  beginRequest(model);
  setGenerationControlsDisabled(true);
  setBusyCartoon("Making both images, one step…");
  try {
    const body = readSettings();
    body.append("photo", model.photo.blob, model.photo.name);
    body.append("scene", getChosenScene());

    const data = await api.generate(body);
    addCalls(model, data.billed_image_count || 2);
    model.generationId = data.generation_id;
    renderScene(data, model, true);
  } catch (error) {
    showCartoonError(error.message);
  } finally {
    endRequest(model);
    setGenerationControlsDisabled(false);
  }
}

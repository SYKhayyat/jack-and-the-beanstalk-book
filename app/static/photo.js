/* Photo sources — anything that can hand the controller a photo Blob.
   Adding a source later (drag-and-drop, say) means adding one object here,
   not touching the controller. Sources receive their elements from the
   view's element map (dependency inversion, so fakes can be stood up easily). */

export function createFilePickerSource(fileInput) {
  return {
    kind: "file-picker",
    /* A File is a Blob, so both sources satisfy the same seam. */
    getBlob() {
      const file = fileInput.files && fileInput.files[0];
      return file || null;
    },
    /* On touch devices the native camera app is a better camera UI than any
       in-page video pane; `capture="user"` asks the picker to open it. */
    useNativeCameraOnTouch(confirm) {
      if (confirm) fileInput.setAttribute("capture", "user");
    },
  };
}

export function createCameraSource(video) {
  let stream = null;
  let lastCaptured = null;

  function hasCameraSupport() {
    return Boolean(
      typeof navigator !== "undefined" &&
        navigator.mediaDevices &&
        typeof navigator.mediaDevices.getUserMedia === "function",
    );
  }

  async function open() {
    if (!hasCameraSupport()) {
      const error = new Error("Camera unsupported in this browser.");
      error.name = "NotSupportedError";
      throw error;
    }
    stopStream();
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: "user", // front camera preferred on phones
        width: { ideal: 1920 },
        height: { ideal: 1920 },
      },
      audio: false,
    });
    video.srcObject = stream;
    try {
      await video.play();
    } catch {
      /* An aborted play (e.g. the pane closed in the same tick) is harmless. */
    }
  }

  /* A still from the live feed, as a JPEG Blob. The frame is captured
     un-mirrored — the mirror on the preview is how people hold themselves,
     the capture is what the camera actually sees. */
  function capture() {
    if (!stream || !video.videoWidth || !video.videoHeight) {
      return Promise.reject(new Error("The camera image is not ready yet."));
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    return new Promise((resolve, reject) => {
      canvas.toBlob(
        (blob) => {
          if (blob) {
            lastCaptured = blob;
            resolve(blob);
          } else {
            reject(new Error("Capturing the frame failed."));
          }
        },
        "image/jpeg",
        0.92,
      );
    });
  }

  function stopStream() {
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
  }

  /* Fully stop every track — the camera indicator light must go off. */
  function close() {
    stopStream();
    if (video.srcObject) video.srcObject = null;
  }

  /* Once a frame has been captured, this source answers the one seam every
     photo source shares: hand me a Blob. */
  function getBlob() {
    return lastCaptured;
  }

  return {
    kind: "camera",
    open,
    capture,
    close,
    getBlob,
    get active() {
      return Boolean(stream);
    },
  };
}

export function isTouchDevice() {
  return typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(pointer: coarse)").matches
    : false;
}

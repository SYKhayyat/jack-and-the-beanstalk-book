/* Client-side downscale: decode → EXIF-respecting resize → JPEG encode.
   A photo already within both limits (pixel bounds and upload size) passes
   through byte-for-byte; only oversized photos and camera frames are
   re-encoded. The original never leaves the device. */

const MAX_LONG_SIDE = 1536;
const JPEG_QUALITY = 0.85;
const OUTPUT_MIME = "image/jpeg";

export class UnreadableImageError extends Error {
  constructor() {
    super("This file could not be read as an image (JPEG, PNG or WEBP).");
    this.name = "UnreadableImageError";
  }
}

/* Draw into an offscreen surface, not the page's DOM; document.createElement
   is only the fallback for browsers without OffscreenCanvas. */
function createCanvas(width, height) {
  if (typeof OffscreenCanvas === "function") {
    return new OffscreenCanvas(width, height);
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

async function decodeViaImageElement(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve({
        source: image,
        width: image.naturalWidth,
        height: image.naturalHeight,
        sourceType: "image-element",
      });
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new UnreadableImageError());
    };
    image.src = url;
  });
}

/* Decode with EXIF orientation applied, so phone photos never come out
   sideways. Falls back to an <img> element where createImageBitmap is missing. */
async function decode(blob) {
  if (typeof createImageBitmap === "function") {
    try {
      const bitmap = await createImageBitmap(blob, {
        imageOrientation: "from-image",
      });
      return {
        source: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        sourceType: "bitmap",
      };
    } catch {
      /* Some formats fail here that the <img> path handles; fall through. */
    }
  }
  return decodeViaImageElement(blob);
}

async function encodeJpeg(source, width, height) {
  const canvas = createCanvas(width, height);
  const context = canvas.getContext("2d");
  context.drawImage(source, 0, 0, width, height);
  if (typeof canvas.convertToBlob === "function") {
    return canvas.convertToBlob({ type: OUTPUT_MIME, quality: JPEG_QUALITY });
  }
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (encoded) => (encoded ? resolve(encoded) : reject(new Error("JPEG encoding failed."))),
      OUTPUT_MIME,
      JPEG_QUALITY,
    );
  });
}

/* Prepare exactly one upload. Returns `{ blob, scaled }` — the blob is the
   thing the caller should preview, remember in state, and POST. Throws
   UnreadableImageError for files the browser cannot decode at all (so the
   error surfaces before any request); any failure in the *scale* path falls
   back to the original bytes and leaves the server-side limit as backstop. */
export async function preparePhoto(photo, { maxBytes = Infinity, force = false } = {}) {
  const decoded = await decode(photo);
  try {
    const longest = Math.max(decoded.width, decoded.height);
    if (!force && longest <= MAX_LONG_SIDE && photo.size <= maxBytes) {
      return { blob: photo, scaled: false, width: decoded.width, height: decoded.height };
    }

    const ratio = Math.min(1, MAX_LONG_SIDE / longest);
    const width = Math.max(1, Math.round(decoded.width * ratio));
    const height = Math.max(1, Math.round(decoded.height * ratio));
    const encoded = await encodeJpeg(decoded.source, width, height);
    return { blob: encoded, scaled: true, width, height };
  } catch (error) {
    /* Ancient browser or an encode hiccup: send the original; the server-side
       "larger than the N MB limit" error still speaks for itself. */
    if (error instanceof UnreadableImageError) throw error;
    return { blob: photo, scaled: false, width: decoded.width, height: decoded.height };
  } finally {
    if (decoded.sourceType === "bitmap") decoded.source.close();
  }
}

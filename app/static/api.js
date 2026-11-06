/* HTTP client — the only module allowed to call fetch. Each wrapper throws on
   a non-ok response, carrying the server's `detail` message across, and the
   HTTP status on the error, so a caller can tell "your session expired" (401)
   or "top up the wallet" (402) from "the model refused". */

function requestError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}

async function readError(response) {
  const data = await response.json().catch(() => ({}));
  const detail = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
  return requestError(detail || `Request failed (${response.status}).`, response.status);
}

export async function postForm(url, body) {
  const response = await fetch(url, { method: "POST", body });
  if (!response.ok) throw await readError(response);
  return response.json().catch(() => ({}));
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw await readError(response);
  return response.json();
}

async function deleteResource(url) {
  const response = await fetch(url, { method: "DELETE" });
  if (!response.ok && response.status !== 404) throw await readError(response);
  return response.status !== 404;
}

/* A draft's images live under /media for its 24-hour life. */
export function draftImageUrl(generationId, stem = "cartoon") {
  return `/media/${generationId}/${stem}`;
}

/* The admin review screen: one press paints one missing plate. */
export function paintMissingPlate(storyKey) {
  return postForm(`/admin/plates/${storyKey}/paint.json`, new FormData());
}

/* The studio: comics, chosen takes, and the drafts behind them. */
export function createStudioClient() {
  return {
    cartoonify: (body) => postForm("/api/cartoonify", body),
    choose: (generationId, index) => {
      const body = new FormData();
      body.append("index", String(index));
      return postForm(`/api/cartoonify/${generationId}/choose`, body);
    },
    /* Is this draft still on the server? Drafts expire after a day. */
    async draftAlive(generationId) {
      try {
        const response = await fetch(draftImageUrl(generationId), { cache: "no-store" });
        return response.ok;
      } catch {
        return false;
      }
    },
    /* The stored original photo, so a retry after a reload needs no re-upload. */
    async draftPhoto(generationId) {
      const response = await fetch(draftImageUrl(generationId, "photo"), { cache: "no-store" });
      if (!response.ok) throw requestError("That photo expired — add it again.", response.status);
      return response.blob();
    },
    startBook: (body) => postForm("/api/book/start", body),
  };
}

/* One existing book: poll it, edit it, throw it away. */
export function createBookClient(bookId) {
  return {
    status: () => getJson(`/api/book/${bookId}`),
    edit: (body) => postForm(`/api/book/${bookId}/edit`, body),
    remove: () => deleteResource(`/api/book/${bookId}`),
    cartoonify: (body) => postForm("/api/cartoonify", body),
  };
}

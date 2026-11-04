/* HTTP client — the only module allowed to call fetch. Each endpoint wrapper
   takes a ready FormData body and throws on non-ok responses, carrying the
   server's `detail` message across, and the HTTP status on the error so a
   caller can tell "your session expired" (401) from "the model refused". */

function requestError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}

export async function postForm(url, body) {
  const response = await fetch(url, { method: "POST", body });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw requestError(data.detail || `Request failed (${response.status}).`, response.status);
  }
  return data;
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw requestError(data.detail || `Request failed (${response.status}).`, response.status);
  }
  return data;
}

async function deleteResource(url) {
  const response = await fetch(url, { method: "DELETE" });
  if (!response.ok && response.status !== 404) {
    const data = await response.json().catch(() => ({}));
    throw requestError(data.detail || `Request failed (${response.status}).`, response.status);
  }
  return response.status === 404 ? false : true;
}

/* The book job is polled, not posted twice — a plain GET wrapper beside the
   POST one keeps `fetch` living in exactly one module. */
export function createBookClient() {
  return {
    start: (body) => postForm("/api/book/start", body),
    status: (bookId) => getJson(`/api/book/${bookId}`),
  };
}

export function createApiClient() {
  return {
    cartoonify: (body) => postForm("/api/cartoonify", body),
  };
}

/* One existing book: poll it, edit it, throw it away. */
export function createBookDetailClient(bookId) {
  return {
    status: () => getJson(`/api/book/${bookId}`),
    edit: (body) => postForm(`/api/book/${bookId}/edit`, body),
    remove: () => deleteResource(`/api/book/${bookId}`),
    /* Any book in the sidebar, not just the one this page is showing. */
    removeOther: (otherId) => deleteResource(`/api/book/${otherId}`),
    cartoonify: (body) => postForm("/api/cartoonify", body),
  };
}

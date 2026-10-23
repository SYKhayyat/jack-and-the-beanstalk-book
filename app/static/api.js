/* HTTP client — the only module allowed to call fetch. Each endpoint wrapper
   takes a ready FormData body and throws on non-ok responses, carrying the
   server's `detail` message across. */

export async function postForm(url, body) {
  const response = await fetch(url, { method: "POST", body });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
}

/* The book job is polled, not posted twice — a plain GET wrapper beside the
   POST one keeps `fetch` living in exactly one module. */
export function createBookClient() {
  return {
    start: (body) => postForm("/api/book/start", body),
    status: async (bookId) => {
      const response = await fetch(`/api/book/${bookId}`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Request failed (${response.status}).`);
      }
      return response.json();
    },
  };
}

export function createApiClient() {
  return {
    cartoonify: (body) => postForm("/api/cartoonify", body),
  };
}

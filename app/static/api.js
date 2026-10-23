/* HTTP client — the only module allowed to call fetch. Each endpoint wrapper
   takes a ready FormData body and returns the parsed JSON response. */

async function postForm(url, body) {
  const response = await fetch(url, { method: "POST", body });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
}

export function createApiClient() {
  return {
    cartoonify: (body) => postForm("/api/cartoonify", body),
    compose: (body) => postForm("/api/compose", body),
    generate: (body) => postForm("/api/generate", body),
  };
}

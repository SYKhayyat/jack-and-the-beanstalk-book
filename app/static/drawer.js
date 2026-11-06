/* My books: a <details> sheet that works without script. This adds the
   manners — Escape and a click outside close it — and the × on each book,
   so a book can be put away without opening it first. */

import { createBookClient } from "./api.js";

const drawer = document.getElementById("books-drawer");

if (drawer) {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawer.open) {
      drawer.open = false;
      drawer.querySelector("summary")?.focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (drawer.open && !drawer.contains(event.target)) drawer.open = false;
  });
  for (const button of drawer.querySelectorAll("[data-delete-book]")) {
    button.addEventListener("click", () => void deleteFromDrawer(button));
  }
}

async function deleteFromDrawer(button) {
  const bookId = button.dataset.deleteBook;
  const title = button.dataset.deleteTitle || "this book";
  if (!window.confirm(`Delete "${title}" and every picture in it? This can't be undone.`)) return;
  button.disabled = true;
  try {
    await createBookClient(bookId).remove();
    // Standing on the book that just went away leaves nowhere to stand.
    if (document.body.dataset.bookId === bookId) {
      window.location.assign("/");
      return;
    }
    button.closest(".drawer-row")?.remove();
  } catch (error) {
    button.disabled = false;
    if (error.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      return;
    }
    window.alert(error.message || "That book couldn't be deleted.");
  }
}

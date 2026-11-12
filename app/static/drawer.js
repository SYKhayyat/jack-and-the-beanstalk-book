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
}

/* The × is not only in the drawer: the library page (Idea 8) puts one on every
   card, and a page that is not the drawer still has to be able to put a book
   away. One listener for every × the page renders. */
for (const button of document.querySelectorAll("[data-delete-book]")) {
  button.addEventListener("click", () => void deleteFromDrawer(button));
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
    // Whichever row the × was on: the drawer's, or a shelf card's.
    (button.closest(".drawer-row") ?? button.closest(".shelf-card"))?.remove();
    // A shelf that just lost its only book should say so, not stand empty.
    const grid = document.querySelector(".shelf-grid");
    if (grid && !grid.querySelector(".shelf-card")) {
      grid.replaceWith(
        Object.assign(document.createElement("p"), {
          className: "library-empty",
          textContent: "No books yet. Start one — it takes a photo and a few minutes.",
        }),
      );
    }
  } catch (error) {
    button.disabled = false;
    if (error.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      return;
    }
    window.alert(error.message || "That book couldn't be deleted.");
  }
}

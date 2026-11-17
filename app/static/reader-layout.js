/* The reader's page geometry: the book's own page shape, sized to the window.

   The reader shows the page the PDF prints, in the size the book was made in
   (reader[data-format]: "square" 8.5 x 8.5, "letter" 8.5 x 11, "wide"
   11 x 8.5). Every leaf is that page: the cover, the copyright page and The
   End are one page of paper each, never wider or taller than a story page.

   - "page" (square and letter): the art across the top at its own shape and
     the words beneath it, in one type size chosen so the longest page fits —
     as the PDF sets them. A page too long for that shrinks the art, not the
     words.
   - "spread" (wide, on a landscape window): the book lies open, the picture
     filling the left page and its words on the facing right page.
   - "single" (wide, on an upright window): one page at a time; turning goes
     picture, then its words (book-live.js steps through the halves).

   Everything is written as CSS custom properties on the reader (--page-w,
   --page-h, --art-h, --story-size) plus data-layout; the stylesheet does the
   rest. Nothing here touches what a page says. */

const MIN_FONT = 16;
const MAX_FONT = 26;
const FACING_MAX_FONT = 30;
const MIN_PAGE = 240;
const TEXT_PAD = 0.075; // the text block's inset, as a share of the page width
const BAND_EXTRA = 2.6; // band height beyond the text, in lines (padding + folio)
const FACING_FILL = 0.8; // how much of a words page the longest text may use
const MIN_ART_SHARE = 0.4; // a long page shrinks its art to no less than this
// Height over width of each format's page, as the PDF prints it.
const PAGE_SHAPE = { square: 1, letter: 11 / 8.5, wide: 8.5 / 11 };

/**
 * @param {HTMLElement} reader  the .reader section
 * @param {HTMLElement} slot    the .book-slot holding every leaf
 * @returns {{ fit: () => void, schedule: () => void, halves: () => boolean }}
 */
export function createReaderLayout(reader, slot) {
  const book = reader.querySelector(".reader-book");
  const bar = reader.querySelector(".reader-bar");
  const texts = Array.from(slot.querySelectorAll('.leaf[data-kind="story"] .story-text'));
  const measurer = document.createElement("div");
  measurer.className = "leaf-text text-measure";
  measurer.setAttribute("aria-hidden", "true");
  const probe = document.createElement("p");
  probe.className = "story-text";
  measurer.append(probe);
  slot.append(measurer);

  let artRatio = parseRatio(getComputedStyle(reader).getPropertyValue("--art-ratio")) || 4 / 3;
  let pending = 0;

  // The first story picture to arrive states the art's true shape (a provider
  // may answer 3:2 to a 4:3 request). Only story art counts: the cover's
  // waiting portrait is 3:4 and says nothing about the pages.
  slot.addEventListener(
    "load",
    (event) => {
      const img = event.target;
      if (!(img instanceof HTMLImageElement) || !img.naturalWidth) return;
      if (!img.matches('.leaf[data-kind="story"] > .leaf-art > img')) return;
      const measured = img.naturalWidth / img.naturalHeight;
      if (Math.abs(measured - artRatio) > 0.01) {
        artRatio = measured;
        reader.style.setProperty("--art-ratio", String(measured));
        schedule();
      }
    },
    true,
  );
  window.addEventListener("resize", schedule);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(schedule);

  function schedule() {
    window.cancelAnimationFrame(pending);
    pending = window.requestAnimationFrame(fit);
  }

  /** The tallest page of story at this text width and size, in px. */
  function tallestText(width, size) {
    measurer.style.width = `${width}px`;
    probe.style.fontSize = `${size}px`;
    let tallest = 0;
    for (const text of texts) {
      probe.textContent = text.textContent;
      tallest = Math.max(tallest, probe.getBoundingClientRect().height);
    }
    return tallest;
  }

  function preferredFont(pageWidth, most) {
    return clamp(Math.round(pageWidth * 0.042), MIN_FONT + 2, most);
  }

  /** The widest page of this shape that fits the space (never below MIN_PAGE). */
  function pageSize(width, height, shape) {
    const page = Math.max(MIN_PAGE, Math.min(width, height / shape));
    return { page, pageH: page * shape };
  }

  /** Square and letter: art on top, the words beneath, on one page. */
  function stacked(width, height, shape) {
    const { page, pageH } = pageSize(width, height, shape);
    const inner = page * (1 - 2 * TEXT_PAD);
    const natural = Math.min(page / artRatio, pageH);
    const bandFor = (size) => tallestText(inner, size) + size * 1.5 * BAND_EXTRA;
    for (let size = preferredFont(page, MAX_FONT); size >= MIN_FONT; size -= 1) {
      if (natural + bandFor(size) <= pageH) return { page, pageH, art: natural, size };
    }
    // A long page keeps the smallest type and gives up art, as the PDF does;
    // past MIN_ART_SHARE the page grows and scrolls — never words cut off.
    const band = bandFor(MIN_FONT);
    const art = Math.max(pageH * MIN_ART_SHARE, Math.min(natural, pageH - band));
    return { page, pageH: Math.max(pageH, art + band), art, size: MIN_FONT };
  }

  /** Wide: each words page is a whole page, so the type can be larger. */
  function facing(page, pageH) {
    const inner = page * (1 - 2 * TEXT_PAD);
    let size = preferredFont(page, FACING_MAX_FONT);
    while (size > MIN_FONT && tallestText(inner, size) > pageH * FACING_FILL) size -= 1;
    return { page, pageH, art: pageH, size };
  }

  function fit() {
    const storyTime = reader.classList.contains("story-time");
    const style = getComputedStyle(book);
    const width =
      book.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const top = storyTime ? 0 : Math.max(0, reader.getBoundingClientRect().top + window.scrollY);
    const chrome = (bar ? bar.offsetHeight : 0) + (storyTime ? 24 : 32);
    const height = Math.max(320, window.innerHeight - Math.min(top, 160) - chrome);

    const format = PAGE_SHAPE[reader.dataset.format] ? reader.dataset.format : "square";
    const shape = PAGE_SHAPE[format];
    let layout = "page";
    let chosen;
    if (format !== "wide") {
      chosen = stacked(width, height, shape);
    } else {
      // Open flat only when the window is landscape and two pages still fit.
      const open = pageSize(width / 2, height, shape);
      if (window.innerWidth > window.innerHeight && width / 2 >= MIN_PAGE) {
        layout = "spread";
        chosen = facing(open.page, open.pageH);
      } else {
        layout = "single";
        const one = pageSize(width, height, shape);
        chosen = facing(one.page, one.pageH);
      }
    }

    reader.dataset.layout = layout;
    reader.style.setProperty("--page-w", `${Math.floor(chosen.page)}px`);
    reader.style.setProperty("--page-h", `${Math.floor(chosen.pageH)}px`);
    reader.style.setProperty("--art-h", `${Math.floor(chosen.art)}px`);
    reader.style.setProperty("--story-size", `${chosen.size}px`);
    // The browser picks the right file from srcset by the size it will show:
    // the picture fills the page's height on a wide book, so ask by that.
    const shownWidth = format === "wide" ? chosen.pageH * artRatio : chosen.page;
    const shown = `${Math.ceil(shownWidth)}px`;
    for (const img of slot.querySelectorAll("img[srcset]")) {
      if (!img.closest(".about-frame")) img.sizes = shown;
    }
  }

  return { fit, schedule, halves: () => reader.dataset.layout === "single" };
}

function parseRatio(value) {
  const text = String(value || "").trim();
  if (!text) return 0;
  const [w, h] = text.split("/").map(Number);
  if (h) return w / h;
  return Number(text) || 0;
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

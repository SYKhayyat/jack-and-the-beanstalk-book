/* The reader's page geometry: one book, sized to the window.

   The PDF sets every story page the same way: the art edge to edge, then a
   band of paper beneath it holding the words, in one type size chosen so the
   longest page fits. The reader does the same, measured against the space the
   window actually has, so the words are never under the fold:

   - "stacked": art on top, the words beneath it (phones, tall windows, and
     the PDF's own shape);
   - "spread": a wide, short window opens the book flat, art on the left page
     and the words on the right, both pages the art's size.

   Everything is written as CSS custom properties on the reader (--page-w,
   --art-h, --band-h, --story-size) plus data-layout; the stylesheet does the
   rest. Nothing here touches what a page says. */

const MIN_FONT = 16;
const MAX_FONT = 26;
const MIN_PAGE = 240;
const SPREAD_MIN_WIDTH = 860;
const TEXT_PAD = 0.075; // the text block's inset, as a share of the page width
const BAND_EXTRA = 2.6; // band height beyond the text, in lines (padding + folio)

/**
 * @param {HTMLElement} reader  the .reader section
 * @param {HTMLElement} slot    the .book-slot holding every leaf
 * @returns {{ fit: () => void, ratio: () => number }}
 */
export function createReaderLayout(reader, slot) {
  const book = reader.querySelector(".reader-book");
  const bar = reader.querySelector(".reader-bar");
  const texts = Array.from(slot.querySelectorAll(".leaf:not(.about-spread) .story-text"));
  const measurer = document.createElement("div");
  measurer.className = "leaf-text text-measure";
  measurer.setAttribute("aria-hidden", "true");
  const probe = document.createElement("p");
  probe.className = "story-text";
  measurer.append(probe);
  slot.append(measurer);

  let artRatio = parseRatio(getComputedStyle(reader).getPropertyValue("--art-ratio")) || 4 / 3;
  let pending = 0;

  // The first picture to arrive states the page's true shape (a provider may
  // answer 3:2 to a 4:3 request); every page of a book has the same one.
  slot.addEventListener(
    "load",
    (event) => {
      const img = event.target;
      if (!(img instanceof HTMLImageElement) || !img.naturalWidth) return;
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

  function preferredFont(pageWidth) {
    return clamp(Math.round(pageWidth * 0.042), MIN_FONT + 2, MAX_FONT);
  }

  /** Stacked: the widest page whose art plus word band fits the height. */
  function stacked(width, height) {
    for (let page = width; page >= MIN_PAGE; page -= Math.max(8, page * 0.04)) {
      const art = page / artRatio;
      const inner = page * (1 - 2 * TEXT_PAD);
      for (let size = preferredFont(page); size >= MIN_FONT; size -= 1) {
        const band = tallestText(inner, size) + size * 1.5 * BAND_EXTRA;
        if (art + band <= height) return { page, art, band, size };
      }
    }
    // Too small a window for any fit: full width, smallest type, and the page
    // scrolls — never words cut off.
    const page = Math.max(MIN_PAGE, width);
    const inner = page * (1 - 2 * TEXT_PAD);
    const band = tallestText(inner, MIN_FONT) + MIN_FONT * 1.5 * BAND_EXTRA;
    return { page, art: page / artRatio, band, size: MIN_FONT };
  }

  /** Spread: two pages of the art's size side by side. */
  function spread(width, height) {
    const page = Math.min(width / 2, height * artRatio);
    const art = page / artRatio;
    const inner = page * (1 - 2 * TEXT_PAD);
    let size = preferredFont(page);
    while (size > MIN_FONT && tallestText(inner, size) > art * 0.82) size -= 1;
    return { page, art, band: art, size };
  }

  function fit() {
    const storyTime = reader.classList.contains("story-time");
    const style = getComputedStyle(book);
    const width =
      book.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const top = storyTime ? 0 : Math.max(0, reader.getBoundingClientRect().top + window.scrollY);
    const chrome = (bar ? bar.offsetHeight : 0) + (storyTime ? 24 : 32);
    const height = Math.max(320, window.innerHeight - Math.min(top, 160) - chrome);

    const flat = stacked(width, height);
    const open = width >= SPREAD_MIN_WIDTH ? spread(width, height) : null;
    const useSpread = open !== null && open.page >= flat.page * 0.8;
    const chosen = useSpread ? open : flat;

    reader.dataset.layout = useSpread ? "spread" : "stacked";
    reader.style.setProperty("--page-w", `${Math.floor(chosen.page)}px`);
    reader.style.setProperty("--art-h", `${Math.floor(chosen.art)}px`);
    reader.style.setProperty("--band-h", `${Math.ceil(chosen.band)}px`);
    reader.style.setProperty("--story-size", `${chosen.size}px`);
    // The browser picks the right file from srcset by the size it will show.
    const shown = `${Math.ceil(chosen.page)}px`;
    for (const img of slot.querySelectorAll("img[srcset]")) {
      if (!img.closest(".about-frame")) img.sizes = shown;
    }
  }

  return { fit, ratio: () => artRatio, schedule };
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

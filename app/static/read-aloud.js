/* "Read to me": the browser reads the page aloud, a sentence at a time, and
   lights up the sentence it is reading so a child can follow the words.

   Uses the browser's own speech (no audio is fetched and nothing leaves the
   device). Where the browser has no speech, the button is never shown. */

const SENTENCE = /[^.!?…]+(?:[.!?…]+[”’"')\]]*|$)\s*/g;
const HEBREW = /[֐-׿]/;

export function speechAvailable() {
  return "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
}

/** Wrap a paragraph's sentences in spans, once, and return them. */
export function sentencesOf(paragraph) {
  if (!paragraph.dataset.sentences) {
    const text = paragraph.textContent || "";
    const parts = text.match(SENTENCE) || [text];
    paragraph.replaceChildren(
      ...parts.map((part) => {
        const span = document.createElement("span");
        span.className = "sentence";
        span.textContent = part;
        return span;
      }),
    );
    paragraph.dataset.sentences = String(parts.length);
  }
  return Array.from(paragraph.querySelectorAll(".sentence"));
}

/**
 * @param {{ onPageRead: () => void }} hooks  called when a page has been read
 *        to its end, so the reader can turn to the next one
 */
export function createReadAloud({ onPageRead }) {
  const synth = window.speechSynthesis;
  let reading = false;
  let run = 0;

  function clearHighlight(root) {
    for (const span of root.querySelectorAll(".sentence.speaking")) span.classList.remove("speaking");
  }

  /** Read every sentence of `parts` (spans, or plain strings) in order. */
  function readParts(parts, root) {
    synth.cancel();
    const mine = (run += 1);
    let position = 0;
    const next = () => {
      if (!reading || mine !== run) return;
      clearHighlight(root);
      if (position >= parts.length) {
        onPageRead();
        return;
      }
      const part = parts[position];
      position += 1;
      const text = typeof part === "string" ? part : part.textContent || "";
      if (!text.trim()) {
        next();
        return;
      }
      if (typeof part !== "string") part.classList.add("speaking");
      const utterance = new SpeechSynthesisUtterance(text.trim());
      utterance.lang = HEBREW.test(text) ? "he-IL" : document.documentElement.lang || "en";
      utterance.rate = 0.9; // a parent's reading pace, not a narrator's
      utterance.onend = next;
      utterance.onerror = next;
      synth.speak(utterance);
    };
    next();
  }

  /** Read one leaf: its story sentences, or the cover's title. */
  function readLeaf(leaf, fallbackText) {
    if (!reading) return;
    const paragraphs = Array.from(leaf.querySelectorAll(".story-text, h2"));
    const parts = paragraphs.flatMap((paragraph) =>
      paragraph.matches("h2") ? [paragraph.textContent || ""] : sentencesOf(paragraph),
    );
    readParts(parts.length ? parts : [fallbackText], leaf);
  }

  function start() {
    reading = true;
  }

  function stop() {
    reading = false;
    run += 1;
    synth.cancel();
    clearHighlight(document);
  }

  return {
    start,
    stop,
    readLeaf,
    get reading() {
      return reading;
    },
  };
}

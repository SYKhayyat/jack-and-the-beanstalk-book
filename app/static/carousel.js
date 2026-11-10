/* Rotating covers: the landing page's deck and each shelf card's fader.

   Markup: a [data-carousel] holding [data-slide] children, the first marked
   .is-active. Optional [data-carousel-prev], [data-carousel-next] and
   [data-carousel-dot="i"] controls. data-interval sets the autoplay pace in
   milliseconds. The slide after the active one is marked .is-next and the one
   before .is-prev, so CSS can stack a deck or cross-fade over an opaque
   cover. Autoplay pauses only while the pointer is actually over the deck
   (never because a dot received focus, which used to strand it paused after
   a click), while the tab is hidden, and never starts for someone who asked
   for reduced motion. data-offset (ms) delays the first turn so cards on a
   shelf turn out of step. */

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

for (const root of document.querySelectorAll("[data-carousel]")) {
  setUp(root);
}

function setUp(root) {
  const slides = [...root.querySelectorAll("[data-slide]")];
  if (slides.length < 2) return;
  const dots = [...root.querySelectorAll("[data-carousel-dot]")];
  const interval = Number(root.dataset.interval) || 5000;
  let current = Math.max(0, slides.findIndex((slide) => slide.classList.contains("is-active")));
  let timer = 0;
  let paused = false;

  const show = (index) => {
    current = (index + slides.length) % slides.length;
    const next = (current + 1) % slides.length;
    const prev = (current - 1 + slides.length) % slides.length;
    slides.forEach((slide, i) => {
      slide.classList.toggle("is-active", i === current);
      /* With two slides the other one is both; it stays "prev" so a fader
         keeps it opaque under the incoming cover instead of blinking out. */
      slide.classList.toggle("is-prev", i === prev && i !== current);
      slide.classList.toggle("is-next", i === next && i !== current && i !== prev);
      if (i === current) slide.removeAttribute("aria-hidden");
      else slide.setAttribute("aria-hidden", "true");
      if (i === next) slide.querySelector("img[loading=lazy]")?.setAttribute("loading", "eager");
    });
    dots.forEach((dot, i) => {
      dot.classList.toggle("is-active", i === current);
      dot.setAttribute("aria-current", i === current ? "true" : "false");
    });
  };

  const stop = () => {
    window.clearInterval(timer);
    timer = 0;
  };
  const start = () => {
    stop();
    if (paused || reducedMotion.matches || document.hidden) return;
    timer = window.setInterval(() => show(current + 1), interval);
  };
  const go = (index) => {
    show(index);
    start();
  };

  root.querySelector("[data-carousel-prev]")?.addEventListener("click", () => go(current - 1));
  root.querySelector("[data-carousel-next]")?.addEventListener("click", () => go(current + 1));
  dots.forEach((dot) => dot.addEventListener("click", () => go(Number(dot.dataset.carouselDot))));
  root.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") go(current - 1);
    if (event.key === "ArrowRight") go(current + 1);
  });

  /* Hover is the only pointer pause. Focus deliberately does not pause: a
     click on a dot or arrow focuses a button inside the deck, and tying the
     timer to focus left the landing carousel stuck after one click. A
     pointer resting on the deck still holds it, as it should. */
  root.addEventListener("pointerenter", () => {
    paused = true;
    stop();
  });
  root.addEventListener("pointerleave", () => {
    paused = false;
    start();
  });
  document.addEventListener("visibilitychange", start);
  reducedMotion.addEventListener?.("change", start);

  /* Cards on one shelf start out of step, so they never all turn at once. */
  const offset = Number(root.dataset.offset) || 0;
  show(current);
  window.setTimeout(start, offset);
}

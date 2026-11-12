/* Turning a page: the leaf lifts from its spine and falls over, with the
   shadow a real page throws.

   Forward, the page you are on peels away to the left and reveals the next
   one beneath it. Back, the previous page falls back over from the left onto
   the one you are on. With reduced motion the page simply changes. */

const TURN_MS = 620;
const EASE = "cubic-bezier(0.33, 0.0, 0.2, 1)";

/**
 * Show `next` in place of `current`. Resolves when the turn is over.
 * A turn started while another is running finishes the running one first.
 */
export function createPageTurner(slot, { reducedMotion }) {
  let running = [];
  let latest = 0;

  function settle() {
    for (const animation of running) animation.finish();
    running = [];
  }

  async function turn(current, next, direction) {
    settle();
    const token = (latest += 1);
    // Only the two leaves of this turn may be on screen.
    for (const leaf of slot.querySelectorAll(".leaf")) {
      leaf.classList.remove("is-turning", "is-beneath");
      if (leaf !== current && leaf !== next) leaf.hidden = true;
    }
    if (!current || current === next || reducedMotion || !next.animate) {
      if (current && current !== next) current.hidden = true;
      next.hidden = false;
      return;
    }
    next.hidden = false;
    const moving = direction === "forward" ? current : next;
    const resting = direction === "forward" ? next : current;
    // The moving leaf is laid over the resting one for the length of the turn.
    moving.classList.add("is-turning");
    resting.classList.add("is-beneath");
    const lifted = [
      { transform: "rotateY(0deg)", filter: "brightness(1)" },
      { transform: "rotateY(-38deg)", filter: "brightness(0.96)", offset: 0.35 },
      { transform: "rotateY(-96deg)", filter: "brightness(0.82)" },
    ];
    const frames = direction === "forward" ? lifted : [...lifted].reverse();
    const shade = resting.animate(
      direction === "forward"
        ? [{ boxShadow: "inset 0 0 80px rgba(24,16,8,0.45)" }, { boxShadow: "inset 0 0 0 rgba(24,16,8,0)" }]
        : [{ boxShadow: "inset 0 0 0 rgba(24,16,8,0)" }, { boxShadow: "inset 0 0 80px rgba(24,16,8,0.45)" }],
      { duration: TURN_MS, easing: EASE },
    );
    const peel = moving.animate(frames, { duration: TURN_MS, easing: EASE });
    running = [peel, shade];
    try {
      await peel.finished;
    } catch {
      // cancelled by a newer turn: the newer turn decides what is shown
    }
    if (token !== latest) return; // a newer turn owns the page now
    moving.classList.remove("is-turning");
    resting.classList.remove("is-beneath");
    current.hidden = true;
    next.hidden = false;
  }

  return { turn, settle };
}

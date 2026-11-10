/* The moment a book is finished: a short shower of paper confetti, once.

   Drawn on a canvas laid over the page for two and a half seconds, then
   removed. Skipped entirely for reduced motion. */

const COLOURS = ["#3c7a4e", "#f2a63b", "#d3566b", "#4f8fc0", "#fdebd0"];
const PIECES = 140;
const DURATION_MS = 2600;

export function celebrate({ reducedMotion }) {
  if (reducedMotion) return;
  const canvas = document.createElement("canvas");
  canvas.className = "confetti";
  canvas.setAttribute("aria-hidden", "true");
  document.body.append(canvas);
  const context = canvas.getContext("2d");
  if (!context) {
    canvas.remove();
    return;
  }
  const scale = window.devicePixelRatio || 1;
  const resize = () => {
    canvas.width = window.innerWidth * scale;
    canvas.height = window.innerHeight * scale;
  };
  resize();
  const pieces = Array.from({ length: PIECES }, () => ({
    x: Math.random() * window.innerWidth,
    y: -20 - Math.random() * window.innerHeight * 0.5,
    size: 6 + Math.random() * 7,
    speed: 2.2 + Math.random() * 3.2,
    drift: (Math.random() - 0.5) * 1.6,
    spin: Math.random() * Math.PI,
    turn: (Math.random() - 0.5) * 0.25,
    colour: COLOURS[Math.floor(Math.random() * COLOURS.length)],
  }));
  const started = performance.now();
  const frame = (now) => {
    const elapsed = now - started;
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.clearRect(0, 0, window.innerWidth, window.innerHeight);
    context.globalAlpha = Math.max(0, 1 - Math.max(0, elapsed - DURATION_MS * 0.7) / (DURATION_MS * 0.3));
    for (const piece of pieces) {
      piece.y += piece.speed;
      piece.x += piece.drift;
      piece.spin += piece.turn;
      context.save();
      context.translate(piece.x, piece.y);
      context.rotate(piece.spin);
      context.fillStyle = piece.colour;
      context.fillRect(-piece.size / 2, -piece.size / 4, piece.size, piece.size / 2);
      context.restore();
    }
    if (elapsed < DURATION_MS) window.requestAnimationFrame(frame);
    else canvas.remove();
  };
  window.requestAnimationFrame(frame);
}

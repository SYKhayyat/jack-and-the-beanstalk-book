/* Shared page chrome: the account menu, and dates in the reader's own timezone.

   Dates are written by the server in UTC and carry a `datetime` attribute.
   This module replaces the fallback text with the same moment in the browser's
   timezone, so a book finished at 19:32 EST reads "9 Nov", not tomorrow's date.
   Anything it cannot parse keeps the server text. */

const dayMonth = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const dayMonthTime = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});
const fullMoment = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

for (const node of document.querySelectorAll("time[datetime]")) {
  const moment = parseMoment(node.getAttribute("datetime"));
  if (!moment) continue;
  const style = node.dataset.format || "day-month";
  node.textContent =
    style === "date-time" ? dayMonthTime.format(moment) : dayMonth.format(moment);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
  node.setAttribute("title", `${fullMoment.format(moment)} (${zone})`);
}

/* A stored timestamp is naive UTC unless it already carries an offset. */
function parseMoment(value) {
  if (!value) return null;
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const moment = new Date(zoned);
  return Number.isNaN(moment.getTime()) ? null : moment;
}

/* Destructive admin forms ask once, in one place. A form opts in with
   data-confirm, so no page needs an inline handler. */
for (const form of document.querySelectorAll("form[data-confirm]")) {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
}

/* The account menu is a <details>, so it works with no script. This only adds
   the manners: Escape and a click outside close it. */
const menu = document.getElementById("user-menu");

if (menu) {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
}

/* A flash message is a notice, not a fixture: it has a close button and
   goes by itself after a few seconds. */
for (const flash of document.querySelectorAll("[data-flash]")) {
  const dismiss = () => {
    flash.classList.add("fading");
    setTimeout(() => flash.remove(), 400);
  };
  flash.querySelector(".flash-close")?.addEventListener("click", dismiss);
  setTimeout(dismiss, 6000);
}

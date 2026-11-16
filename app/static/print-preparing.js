// The print page while a book's print files are made in the background:
// look again every few seconds until the server shows the next step.
const WAIT_MS = 4000;

if (document.getElementById("print-preparing")) {
  setTimeout(() => window.location.replace(window.location.pathname), WAIT_MS);
}

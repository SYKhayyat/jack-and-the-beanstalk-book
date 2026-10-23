/* Model: photo and generation state.
   Pure data plus the small pure functions that touch it — zero DOM references
   and no network, so it runs unchanged inside tests or with fake wiring. */

export function createState() {
  return {
    /* What actually gets uploaded: `{ blob, name }`, already downscaled. */
    photo: null,
    generationId: null,
    calls: 0,
    requestInFlight: false,
    candidateUrls: [],
    chosen: 0,
    attempts: [],
  };
}

export function setPhoto(state, blob, name) {
  state.photo = { blob, name };
}

export function clearPhoto(state) {
  state.photo = null;
}

export function chooseCandidate(state, index) {
  state.chosen = index;
}

export function addCalls(state, count) {
  state.calls += count;
}

export function addAttempt(state, attempt) {
  state.attempts.push(attempt);
}

export function beginRequest(state) {
  state.requestInFlight = true;
}

export function endRequest(state) {
  state.requestInFlight = false;
}

/* Start over: the generation history goes, the chosen photo stays — a new
   cartoon with the same source is the most common restart. */
export function resetGeneration(state) {
  state.generationId = null;
  state.calls = 0;
  state.candidateUrls = [];
  state.chosen = 0;
  state.attempts = [];
}

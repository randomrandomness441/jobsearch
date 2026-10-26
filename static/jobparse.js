'use strict';
/* Pure text parsing for the "Add job manually" paste box -- no DOM, so this
 * is Node-require()-able for real tests (see test_jobparse.js) without
 * pulling in the rest of app.js, which touches `document` at the top level
 * and can't be loaded outside a browser. */

// A real job title is short -- "Staff Software Engineer, Payments
// Infrastructure" is 46 characters. When a pasted posting's "first line"
// comes out far longer than this, no real line break actually separated a
// title from the body (a common copy-paste artifact from pages that render
// a title and the rest of the posting without a literal newline between
// them) -- treating that whole blob as the title is exactly the bug this
// guards against: the title field silently ends up holding the entire
// description. 120 chars comfortably covers real titles (including a
// trailing location aside) with margin before anything resembling a run of
// description prose.
const MAX_TITLE_LEN = 120;

// First non-empty line is the title candidate; the next one is the location
// IF it looks like one (short, and either "Remote"/"Hybrid"/India-ish, or a
// plain "City, Country" with no sentence punctuation) -- otherwise it's
// already prose and there's no separate location line to pull out.
//
// If the title candidate is implausibly long (see MAX_TITLE_LEN), this is
// a *guess*, not a confident parse -- titleUncertain is set so the caller
// can flag it for the user to check/fix rather than silently using a
// corrupted value. The guess itself is still truncated at a word boundary
// (never the raw multi-hundred-character blob), so even an unfixed import
// never ends up with the whole description sitting in the title field.
function parseJobPaste(raw) {
  const lines = raw.split('\n').map(l => l.trim());
  let i = 0;
  while (i < lines.length && !lines[i]) i++;
  let title = lines[i] || '';
  let titleUncertain = false;
  if (title.length > MAX_TITLE_LEN) {
    titleUncertain = true;
    title = title.slice(0, MAX_TITLE_LEN).replace(/\s+\S*$/, '').trim();
  }
  let j = i + 1;
  while (j < lines.length && !lines[j]) j++;
  const candidate = lines[j] || '';
  const looksLikeLocation = candidate && candidate.length <= 60 &&
    (/remote|hybrid|on-?site|india|bangalore|bengaluru/i.test(candidate) ||
     (candidate.includes(',') && !candidate.endsWith('.')));
  return { title, location: looksLikeLocation ? candidate : '', titleUncertain };
}

// Node-testable; a no-op in the browser (module is undefined there).
if (typeof module !== 'undefined') module.exports = { parseJobPaste, MAX_TITLE_LEN };

#!/usr/bin/env node
/* Tests for parseJobPaste (static/app.js) -- the title/location guesser for
 * the "Add job manually" paste box. Plain node + assert, no framework,
 * matching this project's no-dependency-beyond-stdlib style on the Python
 * side.
 *
 *     node test_jobparse.js
 *
 * Exit code 0 if every test passes, 1 otherwise (prints which failed).
 *
 * Covers the real bug reported live: pasting a posting whose title and
 * description arrived with no real line break between them (a common
 * copy-paste artifact from pages that don't preserve newlines) ended up
 * with the ENTIRE description sitting in the title field -- MAX_TITLE_LEN
 * plus the truncate-at-overlong-first-line logic is the fix; these tests
 * are what prove it actually holds, not just reasoning about the code.
 */
const assert = require('assert');
const { parseJobPaste, MAX_TITLE_LEN } = require('./static/jobparse.js');

let passed = 0, failed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`PASS: ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL: ${name}\n  ${e.message}`);
  }
}

test('normal paste: title line 1, location line 2, description after', () => {
  const raw = 'Software Development Engineer II\nRemote (India)\nAbout Us...\nWe build things.';
  const { title, location, titleUncertain } = parseJobPaste(raw);
  assert.strictEqual(title, 'Software Development Engineer II');
  assert.strictEqual(location, 'Remote (India)');
  assert.strictEqual(titleUncertain, false);
});

test('normal paste with no separate location line -- description starts immediately', () => {
  const raw = 'Backend Engineer\nWe are looking for someone who can own our payments stack end to end.';
  const { title, location } = parseJobPaste(raw);
  assert.strictEqual(title, 'Backend Engineer');
  // The second line reads as prose (long, ends mid-sentence, no comma-separated
  // place), not a location -- must not be misdetected as one.
  assert.strictEqual(location, '');
});

test('leading blank lines before the title are skipped', () => {
  const raw = '\n\n  \nStaff Engineer\nBangalore, India\nJob description text here.';
  const { title, location } = parseJobPaste(raw);
  assert.strictEqual(title, 'Staff Engineer');
  assert.strictEqual(location, 'Bangalore, India');
});

test('location line correctly rejected when it is actually prose, not a place', () => {
  const raw = 'Product Manager\nWe need someone who thrives in ambiguity and loves shipping fast.';
  const { location } = parseJobPaste(raw);
  assert.strictEqual(location, '');
});

test('title line right at the boundary (<= MAX_TITLE_LEN) passes through unchanged', () => {
  const title46 = 'Staff Software Engineer, Payments Infrastructure'; // 49 chars, still realistic
  assert.ok(title46.length <= MAX_TITLE_LEN, 'fixture should be under the cap for this test to mean anything');
  const raw = `${title46}\nRemote\nDescription...`;
  const { title, titleUncertain } = parseJobPaste(raw);
  assert.strictEqual(title, title46);
  assert.strictEqual(titleUncertain, false);
});

test('THE REPORTED BUG: no real line break between title and description -- '
     + 'title must NOT become the entire pasted text', () => {
  const longDescription = 'We are a fast growing fintech company building the next generation of '
    + 'payment infrastructure for small businesses across the region, and we are looking for an '
    + 'experienced backend engineer to join our platform team and help us scale to millions of '
    + 'transactions per day while maintaining five nines of reliability across every service.';
  const raw = `Senior Backend Engineer ${longDescription}`; // one single line, no newline at all
  const { title, titleUncertain } = parseJobPaste(raw);
  assert.ok(title.length <= MAX_TITLE_LEN,
    `title must be capped at ${MAX_TITLE_LEN} chars, got ${title.length}: ${JSON.stringify(title)}`);
  assert.notStrictEqual(title, raw, 'title must never equal the entire raw paste');
  assert.ok(!title.includes('transactions per day'),
    'title must not contain text from deep in the description');
  assert.strictEqual(titleUncertain, true,
    'an overlong first line must be flagged as an uncertain guess, not a confident parse');
});

test('overlong single line truncates at a word boundary, not mid-word', () => {
  const raw = 'A '.repeat(80) + 'Engineer'; // 160 chars, no newline
  const { title } = parseJobPaste(raw);
  assert.ok(title.length <= MAX_TITLE_LEN);
  assert.ok(!/\S$/.test(title) === false, 'title should end in a real character, not be empty');
  // Truncation must land on a space boundary -- the slice+regex strips any
  // trailing partial word, so the result never ends mid-token.
  assert.ok(raw.startsWith(title.replace(/\s+$/, '')),
    'truncated title must be an exact, unmangled prefix of the original line');
});

test('empty paste does not throw, returns empty title/location', () => {
  const { title, location } = parseJobPaste('');
  assert.strictEqual(title, '');
  assert.strictEqual(location, '');
});

test('whitespace-only paste does not throw, returns empty title/location', () => {
  const { title, location } = parseJobPaste('   \n   \n   ');
  assert.strictEqual(title, '');
  assert.strictEqual(location, '');
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);

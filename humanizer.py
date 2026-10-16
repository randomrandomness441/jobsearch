"""Deterministic, code-level enforcement of HUMAN_STYLE_RULES
(platform/server.py in the C++ repo) against generated resume/cover-letter
text. Before this, HUMAN_STYLE_RULES was only a comment telling whoever
writes the text (Claude, per job) to follow it -- nothing in code ever
checked. This is the real gate: generate() calls check_human_style() on
both the summary and the cover letter body, and refuses to render past a
violation instead of trusting it was followed.
"""
import re

BANNED_WORDS = ["delve", "leverage", "underscore", "boast", "robust", "seamless"]

# Hedging/filler phrases HUMAN_STYLE_RULES explicitly names. Checked as
# plain substrings on lowercased text -- these are specific enough that a
# false-positive match in real resume/cover-letter prose is vanishingly
# unlikely.
BANNED_PHRASES = [
    "let me know if you have any questions",
    "can be complex",
    "it's worth noting that",
    "it is worth noting that",
    "furthermore", "moreover", "additionally,",
]

# Real em dash, plus the "--" stand-in -- this codebase's own comments use
# "--" constantly, but that's a code-comment convention, not something that
# should ever land in text a human reads as a resume or cover letter.
_DASH_RE = re.compile(r"—|--")

# "it's not just X, it's Y" -- the specific construction HUMAN_STYLE_RULES
# bans by name. Matches "not just ... it's/but" within the same sentence,
# not a bare "not" anywhere (which would false-positive constantly).
_NOT_JUST_RE = re.compile(r"\bnot\s+just\b.{0,60}?\b(it'?s|it\s+is|but)\b", re.I)

# "write so a 12-year-old / someone outside the specialty could follow it"
# has only ever been prompt text (Call 2's system prompt, this module's own
# docstring above) -- nothing in code ever checked it, same gap this whole
# module exists to close for AI-writing tells. These two checks don't try
# to be a real NLP readability model (named systems/tech terms are long
# "words" by any syllable-counting formula, so a strict Flesch-Kincoid gate
# would false-positive on perfectly clear technical bullets) -- they check
# the two concrete things plain-language guides agree are the actual
# readability levers: how long a single sentence runs, and how many
# clauses got stacked into it. A reader gives a resume seconds, not
# re-reads; a sentence that needs a second pass has already failed.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
LONG_SENTENCE_WORD_LIMIT = 35     # plain-language guidance: ~20-25 is easy, 30+ is hard
MAX_COMMAS_PER_SENTENCE = 4        # HUMAN_STYLE_RULES already says "one idea per sentence" --
                                    # was 3, loosened after a real failure: "detector (~98% accurate)
                                    # using embeddings, a vector database and semantic search over
                                    # metric definitions, so the platform reuses..." is a legitimate
                                    # tech-enumeration-plus-outcome shape (3 items + one "so" clause
                                    # naturally needs 4 commas), not a run-on; 3 was catching reasonable
                                    # sentences, not just genuinely stuffed ones.
                                   # -- this is the code-level version of that rule


def _sentence_complexity_violations(text):
    violations = []
    for sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        word_count = len(sentence.split())
        if word_count > LONG_SENTENCE_WORD_LIMIT:
            violations.append(
                f'sentence is {word_count} words, over the {LONG_SENTENCE_WORD_LIMIT}-word plain-language '
                f'limit -- split it, a reader won\'t parse this in one pass: "{sentence[:70]}..."')
        comma_count = sentence.count(",")
        if comma_count > MAX_COMMAS_PER_SENTENCE:
            violations.append(
                f'sentence stacks {comma_count} commas -- too many clauses in one sentence for a quick '
                f'read, split it into separate sentences: "{sentence[:70]}..."')
    return violations


# Found live, real and alarming: a Z.ai reply came back with every resume bullet written
# in Chinese instead of English. Nothing in this gate checked LANGUAGE at all -- length and
# comma checks on non-Latin text are close to meaningless (CJK text has no spaces, so
# `.split()` undercounts "words" wildly, and the comma check only looks for the ASCII ","),
# so a non-English reply could sail through clean. This is a hard, non-negotiable gate: any
# meaningful share of non-ASCII-letter characters fails immediately, no exceptions for
# legitimate non-English content, since every real caller here is English-only resume/
# cover-letter text. Allows accented Latin letters (names, "café") and the handful of
# specific Unicode punctuation marks this codebase's own style already uses on purpose
# (curly quotes, the real em dash this module separately bans) without flagging them twice.
_NON_LATIN_RE = re.compile(r"[^\x00-\x7FÀ-ɏ‐-―‘-‟]")
NON_LATIN_FRACTION_LIMIT = 0.15   # real accented words/names are a few characters, not a wall of them


def _language_violations(text):
    if not text:
        return []
    non_latin = len(_NON_LATIN_RE.findall(text))
    if non_latin / len(text) > NON_LATIN_FRACTION_LIMIT:
        return [f"text is not in English ({non_latin}/{len(text)} characters are non-Latin script) -- "
                f"reply in English only: \"{text[:70]}...\""]
    return []


def check_human_style(text):
    """Returns a list of plain-English violation descriptions -- empty
    means clean. Call this on every piece of text before it gets rendered;
    treat a non-empty result as a hard stop, not a warning."""
    violations = []
    violations.extend(_language_violations(text))
    if violations:
        # A non-English reply makes every other check (word-boundary regexes, sentence
        # splitting) unreliable on top of already being disqualifying -- report just this
        # and stop, rather than piling on confusing or wrong secondary violations.
        return violations
    if _DASH_RE.search(text):
        violations.append("contains an em dash (or a \"--\" stand-in) -- use a period or a comma instead")
    low = text.lower()
    for w in BANNED_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low):
            violations.append(f'uses the banned word "{w}"')
    for p in BANNED_PHRASES:
        if p in low:
            violations.append(f'uses the banned phrase "{p}"')
    if _NOT_JUST_RE.search(text):
        violations.append('uses the banned "it\'s not just X, it\'s Y" construction')
    violations.extend(_sentence_complexity_violations(text))
    return violations

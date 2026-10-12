"""Cleans up the raw transcript with gpt-oss-20b (same call as test_record.py)."""

import re
from collections import Counter

from app import api
from app.dictionary import cleanup_dictionary_line
from app.paths import PROMPT_PATH

MODEL = "openai/gpt-oss-20b"
# Cleanup only removes words. If it comes back much longer than what you said, the model
# answered or added something (or was talked into it by other speech), so the raw
# transcript is pasted instead. Allowed: twice the words, plus this many.
MAX_EXTRA_WORDS = 10
# The other way round: cleanup dropped real content (a whole sentence, when you said
# something twice in other words). Fillers, tiny words and number words don't count, since
# cleanup removes or rewrites those on purpose ("twenty five" becomes "25"). It counts as
# dropped when at least this many of your real words are missing...
DROPPED_MIN_WORDS = 8
# ...and they're at least this share of all your real words. A short "Monday, no wait,
# Tuesday" never gets there; a lost sentence in a longer dictation does.
DROPPED_MIN_SHARE = 0.25
# The third way (M17): a short dictation answered instead of cleaned ("can you check the logs"
# came back as "Sure, I'll check the logs now."). It's too short for MAX_EXTRA_WORDS, and nothing
# was dropped. It counts as answered when the result is longer than what you said and has at
# least this many real words you never said...
ADDED_MIN_WORDS = 2
# ...that are at least this share of your own real words. A fixed misheard word or two in a
# longer dictation never gets there.
ADDED_MIN_SHARE = 0.5
# Only dictations of at least this many words can count as answered. A 1 or 2 word dictation
# (a mic test, a name) has so few words that any fix looks like a big share of new words, and
# a question worth answering is longer anyway.
ANSWERED_MIN_RAW_WORDS = 3
# A word cleanup split off a glued one ("mictesting" becomes "mic testing") was said: a result
# word of at least this many letters inside one of your words counts as said. Shorter pieces
# ("a", "at") sit inside too many words to mean anything.
SPLIT_MIN_LETTERS = 3
# Casual words cleanup may write out ("gonna" becomes "going to"): the written form counts as said.
CASUAL_FORMS = {
    "gonna": ("going",), "wanna": ("want",), "gotta": ("got",), "lemme": ("let",), "gimme": ("give",),
    "dunno": ("know",), "outta": ("out",), "hafta": ("have",), "cause": ("because",), "cuz": ("because",),
    "til": ("until",), "till": ("until",), "em": ("them",),
}

# Words that don't count as content: "So." or "Um, uh" alone isn't worth pasting (pipeline.py),
# and cleanup may drop them without it counting as lost content.
FILLER_WORDS = {"so", "um", "umm", "uh", "uhh", "uhm", "hmm", "mm", "er", "erm", "ah", "like", "you", "know"}
TINY_WORDS = {
    "a", "an", "the", "i", "me", "my", "we", "us", "our", "it", "its", "is", "am", "are", "was", "were", "be",
    "been", "to", "of", "in", "on", "at", "for", "and", "or", "but", "not", "no", "yes", "okay", "ok", "oh",
    "that", "this", "just", "then", "well", "s", "t", "m", "re", "ll", "ve", "d", "do", "don", "actually",
    "wait", "mean", "sorry", "right", "yeah", "basically", "really", "kind", "sort",
}
NUMBER_WORDS = {
    "zero", "oh", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
    "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
    "billion", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "point", "percent", "o", "clock", "am", "pm", "number", "dollars", "dollar", "half", "quarter",
}
SKIPPED_WORDS = FILLER_WORDS | TINY_WORDS | NUMBER_WORDS
EMPTY = "the result was empty"
TAG = re.compile(r"<\s*/?\s*transcript\s*>", re.IGNORECASE)  # our wrapper's tags, in any case or spacing


def too_long(raw, cleaned):
    """True if cleaned has more than twice raw's words plus MAX_EXTRA_WORDS."""
    return len(cleaned.split()) > 2 * len(raw.split()) + MAX_EXTRA_WORDS


def content_words(text):
    """The words that carry meaning, lowercased. Digits don't count (cleanup writes numbers
    its own way), so "testing1234" counts as "testing"."""
    return [w for w in re.findall(r"[^\W\d_]+", text.lower()) if w not in SKIPPED_WORDS]


def dropped_words(raw, cleaned):
    """Returns (how many of raw's content words are missing from cleaned, how many raw has).

    Counted with repeats: a restated sentence often reuses words from the one that stayed."""
    have = Counter(content_words(cleaned))
    wanted = Counter(content_words(raw))
    missing = sum(max(0, n - have[w]) for w, n in wanted.items())
    return missing, sum(wanted.values())


def dropped_content(raw, cleaned):
    """True if cleanup seems to have dropped real content (see DROPPED_MIN_WORDS)."""
    missing, total = dropped_words(raw, cleaned)
    return missing >= DROPPED_MIN_WORDS and missing >= DROPPED_MIN_SHARE * total


def added_words(raw, cleaned, terms=()):
    """Returns (how many of cleaned's content words you never said, how many raw has).

    Counted with repeats, like dropped_words. Not counted as new: dictionary terms (cleanup
    fixes "grog" to "Groq" on purpose), the written form of a casual word you said (CASUAL_FORMS),
    and a piece of a glued word you said that cleanup split (SPLIT_MIN_LETTERS)."""
    raw_words = content_words(raw)
    said = Counter(raw_words)
    total = sum(said.values())
    for word, n in list(said.items()):
        for form in CASUAL_FORMS.get(word, ()):
            said[form] += n
    allowed = {word for term in terms for word in content_words(term)}
    have = Counter(content_words(cleaned))
    added = 0
    for word, n in have.items():
        if word in allowed:
            continue
        # Only for a word never said whole, so repeats of whole words still count as before.
        if not said[word] and len(word) >= SPLIT_MIN_LETTERS and any(word in w for w in raw_words):
            continue
        added += max(0, n - said[word])
    return added, total


def answered(raw, cleaned, terms=()):
    """True if cleanup seems to have answered you instead of cleaning up (see ADDED_MIN_WORDS)."""
    if len(raw.split()) < ANSWERED_MIN_RAW_WORDS:
        return False
    if len(cleaned.split()) <= len(raw.split()):
        return False
    added, total = added_words(raw, cleaned, terms)
    return added >= ADDED_MIN_WORDS and added >= ADDED_MIN_SHARE * total


def wrap_transcript(raw):
    """The user message: the transcript inside <transcript>...</transcript>. Any tag text in the
    transcript itself is taken out first, so it can't end the transcript early and leave the
    rest looking like instructions (check_result strips the same tags from the answer)."""
    return f"<transcript>{TAG.sub('', raw)}</transcript>"


def build_system_prompt(terms, prompt_text):
    """The system message: the prompt, plus the dictionary line when there are terms."""
    return prompt_text.strip() + cleanup_dictionary_line(terms)


def check_result(raw, cleaned, terms=()):
    """The safety nets, in case the model breaks its rules. Returns (text or None, reason or None).

    None means the raw transcript should be pasted instead; reason says why, for the console
    (word counts only, never the words). terms: the dictionary, for answered()."""
    cleaned = TAG.sub("", cleaned)
    cleaned = re.sub(r"\s*—\s*", ", ", cleaned)
    # A Markdown line break (spaces, then a new line) means nothing in a plain text box: the
    # model sometimes puts one between sentences, so join those lines. A plain new line stays.
    cleaned = re.sub(r"[ \t]{2,}\r?\n[ \t]*", " ", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return None, EMPTY
    if too_long(raw, cleaned):
        return None, (f"the result is much longer than what you said ({len(cleaned.split())} words for "
                      f"{len(raw.split())}), so it isn't used")
    if dropped_content(raw, cleaned):
        missing, total = dropped_words(raw, cleaned)
        return None, f"the result is missing {missing} of your {total} words, so it isn't used"
    if answered(raw, cleaned, terms):
        added, _ = added_words(raw, cleaned, terms)
        return None, (f"the result adds {added} words you didn't say ({len(cleaned.split())} words for "
                      f"{len(raw.split())}), so it isn't used")
    return cleaned, None


def ask_model(client, tracker, raw, terms, prompt_path=None):
    """Sends the transcript to the model. Returns (its answer, as is, and the tokens counted).

    Raises api.RateLimited, or the SDK's error. prompt_path replaces prompts/cleanup.txt
    (tools/cleanup_eval.py tries drafts with it).
    """
    # Read fresh every time so the prompt can be tuned while the app runs.
    system = build_system_prompt(terms, (prompt_path or PROMPT_PATH).read_text(encoding="utf-8"))

    def send():
        return client.chat.completions.with_raw_response.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": wrap_transcript(raw)},
            ],
            reasoning_effort="low",
            temperature=0.2,
        )

    response, latency_ms = api.request(tracker, "cleanup", send)
    parsed = response.parse()
    tokens = tracker.log_cleanup(latency_ms, parsed.usage, response.headers)
    return parsed.choices[0].message.content or "", tokens


def clean_up(client, tracker, raw, terms):
    """Returns (cleaned text, status, tokens).

    status is "ok", "limited" (rate limit) or "failed". On anything but "ok" the
    text is None and the caller pastes the raw transcript instead.
    tokens is what counted toward the daily token limit (0 if it failed).
    """
    try:
        answer, tokens = ask_model(client, tracker, raw, terms)
    except api.RateLimited as e:
        print(f"cleanup: {e}")
        return None, "limited", 0
    except Exception as e:
        print(f"cleanup error: {api.describe_for_log(e)}")
        return None, "failed", 0

    cleaned, reason = check_result(raw, answer, terms)
    if cleaned is None:
        if reason != EMPTY:
            print(f"cleanup: {reason}")
        return None, "failed", tokens
    return cleaned, "ok", tokens

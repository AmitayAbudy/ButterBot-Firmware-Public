"""
English text -> the robot's speech-recognition phonemes (MultiNet7's alphabet), for custom voice commands.

Words come from the CMU Pronouncing Dictionary (phonemes.txt.gz, made by gen_phonemes.py). A word it doesn't know is
split into known parts if it can be ("butterbot" = butter + bot), else guessed from its spelling - to_phonemes reports
those so the app can suggest teaching the phrase by voice instead.
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

MAX_LEN = 63          # esp-sr ESP_MN_MAX_PHRASE_LEN
_dict: dict[str, str] | None = None

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
       "seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()

# Spelling rules for words the dictionary doesn't have, longest first: (letters, phonemes)
RULES = sorted([
    ("tion", "scN"), ("sion", "rcN"), ("ight", "iT"), ("ough", "c"), ("eigh", "d"), ("augh", "e"),
    ("igh", "i"), ("tch", "p"), ("dge", "q"), ("sch", "SK"), ("que", "K"),
    ("ch", "p"), ("sh", "s"), ("th", "v"), ("ph", "F"), ("ng", "l"), ("ck", "K"), ("qu", "KW"), ("wh", "W"),
    ("kn", "N"), ("wr", "R"), ("gh", "G"), ("ee", "m"), ("ea", "m"), ("oo", "o"), ("ai", "d"), ("ay", "d"),
    ("oa", "b"), ("ow", "b"), ("ou", "t"), ("oi", "u"), ("oy", "u"), ("er", "k"), ("ir", "k"), ("ur", "k"),
    ("ar", "nR"), ("or", "eR"), ("aw", "e"), ("au", "e"), ("ie", "m"), ("ey", "m"), ("ue", "o"), ("ew", "o"),
    ("ll", "L"), ("ss", "S"), ("tt", "T"), ("pp", "P"), ("bb", "B"), ("dd", "D"), ("ff", "F"), ("gg", "G"),
    ("mm", "M"), ("nn", "N"), ("rr", "R"), ("zz", "Z"), ("cc", "K"),
    ("a", "a"), ("e", "f"), ("i", "g"), ("o", "n"), ("u", "c"), ("b", "B"), ("d", "D"), ("f", "F"), ("g", "G"),
    ("h", "h"), ("j", "q"), ("k", "K"), ("l", "L"), ("m", "M"), ("n", "N"), ("p", "P"), ("r", "R"), ("s", "S"),
    ("t", "T"), ("v", "V"), ("w", "W"), ("x", "KS"), ("z", "Z"),
], key=lambda r: -len(r[0]))


def _load() -> dict[str, str]:
    global _dict
    if _dict is None:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        _dict = {}
        with gzip.open(base / "phonemes.txt.gz", "rt", encoding="ascii") as f:
            for line in f:
                w, _, p = line.rstrip("\n").partition("\t")
                _dict[w] = p
    return _dict


def _number(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _number(n % 100))
    return " ".join(ONES[int(d)] for d in str(n))


def words_of(text: str) -> list[str]:
    """Lower-case words, numbers spelled out, everything else dropped."""
    text = re.sub(r"\d+", lambda m: " " + _number(int(m.group())) + " ", text.lower().replace("&", " and "))
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text)


def _spell(word: str) -> str:
    """A guess from the spelling: silent final e, "y" at the end said "ee", "c" soft before e/i/y."""
    w = word.replace("'", "")
    if len(w) > 3 and w.endswith("e") and not w.endswith("ee"):
        w = w[:-1]
    out, i = "", 0
    while i < len(w):
        if w[i] == "c":
            out += "S" if i + 1 < len(w) and w[i + 1] in "eiy" else "K"
            i += 1
            continue
        if w[i] == "y":
            out += "Y" if i == 0 else ("m" if i == len(w) - 1 else "i")
            i += 1
            continue
        for letters, ph in RULES:
            if w.startswith(letters, i):
                out += ph
                i += len(letters)
                break
        else:
            i += 1
    return out


def _word(word: str, d: dict[str, str]) -> tuple[str, bool]:
    """(phonemes, known): known = from the dictionary, whole or in parts."""
    if word in d:
        return d[word], True
    for suffix, extra in (("'s", "Z"), ("s", "Z"), ("ing", "gl"), ("ed", "D")):
        if word.endswith(suffix) and word[:-len(suffix)] in d:
            return d[word[:-len(suffix)]] + extra, True
    # Two known parts (longest first part wins): "butterbot", "goodnight"
    for cut in range(len(word) - 2, 1, -1):
        a, b = word[:cut], word[cut:]
        if a in d and b in d and len(b) > 1:
            return d[a] + d[b], True
    return _spell(word), False


def to_phonemes(text: str) -> tuple[str, list[str]]:
    """The phrase's phonemes (words separated by spaces, at most MAX_LEN characters) and the words that were
    guessed from their spelling. Raises ValueError when the phrase is empty or too long for the robot."""
    d = _load()
    words = words_of(text)
    if not words:
        raise ValueError("type a few words")
    parts, guessed = [], []
    for w in words:
        ph, known = _word(w, d)
        if ph:
            parts.append(ph)
        if not known:
            guessed.append(w)
    out = " ".join(parts)
    if len(out) > MAX_LEN:
        raise ValueError("too long for the robot - use fewer or shorter words")
    return out, guessed

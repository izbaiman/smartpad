"""Offline spelling correction (English) used when no AI key is configured.

Conservative by design - it only changes a word when:
  * the word is purely alphabetic, 3+ letters, not an ALL-CAPS acronym,
  * the word is not in the dictionary, and
  * the dictionary offers a clearly better candidate (edit distance <= 2).
Words the user writes repeatedly (likely names/terms) are left alone.
"""
from __future__ import annotations

import re
from collections import Counter

_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_checker = None


def _get():
    global _checker
    if _checker is None:
        try:
            from spellchecker import SpellChecker
        except ImportError:
            return None
        _checker = SpellChecker(language="en", distance=2)
    return _checker


def available() -> bool:
    return _get() is not None


def _match_case(src: str, dst: str) -> str:
    if src.isupper():
        return dst.upper()
    if src[0].isupper():
        return dst[0].upper() + dst[1:]
    return dst


def correct(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Returns (corrected_text, [(wrong, right), ...])."""
    sc = _get()
    if sc is None or not text:
        return text, []
    words = _WORD.findall(text)
    counts = Counter(w.lower() for w in words)
    unknown = sc.unknown([w.lower() for w in words if len(w) >= 3 and "'" not in w])
    fixes: dict[str, str] = {}
    for w in unknown:
        if counts[w] > 2:  # repeated unusual word: probably a name or technical term
            continue
        cand = sc.correction(w)
        if cand and cand != w and cand.isalpha():
            fixes[w] = cand

    changes: list[tuple[str, str]] = []

    def repl(m: re.Match) -> str:
        w = m.group(0)
        if len(w) > 1 and w.isupper():
            return w  # acronym
        new = fixes.get(w.lower())
        if not new:
            return w
        out = _match_case(w, new)
        changes.append((w, out))
        return out

    corrected = _WORD.sub(repl, text)
    corrected = re.sub(r"\bi\b", "I", corrected)
    return corrected, changes

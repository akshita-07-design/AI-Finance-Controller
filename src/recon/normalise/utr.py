"""
UTR extraction and confusion-aware canonicalization.

Two separate concerns that live together because they're both about the
same underlying problem: recovering a UTR from messy free text.

  - `extract_utr_candidates`: pull anything UTR-shaped out of a bank
    narration or ref_no field. May return a string that LOOKS like a UTR but
    is actually corrupted (wrong content, right shape) — that's expected;
    Pass 1 relies on returning something exactly equal to a known good UTR,
    Pass 4 relies on returning something close.

  - `canonicalize`: collapse the specific confusable-character families
    (0/O, 1/I, 5/S) to a single representative BEFORE computing edit
    distance. This is what makes Pass 4's fuzzy match "confusion-aware"
    rather than just generic Levenshtein — a narration that only differs
    from the true UTR by these swaps collapses to edit distance 0 after
    canonicalization, while a genuinely different UTR does not.
"""

from __future__ import annotations

import re

# Ordered by specificity — try the most distinctive shape first so a
# "RZRP..." style UTR isn't accidentally matched by the more permissive
# pure-numeric pattern that comes after it.
_UTR_PATTERNS = [
    re.compile(r"RZRP\d{10,14}"),                     # RZRP173069230702
    re.compile(r"\d{10}[a-zA-Z0-9]{4,8}"),             # 1568176960vxp0rj
    re.compile(r"\b\d{10,16}\b"),                       # pure numeric IMPS-style
    # Permissive fallback: any sufficiently long alnum blob, no digit-count
    # requirement. Needed because a single confusable-character swap INSIDE
    # the rigid \d{10} prefix above (e.g. the 4th digit corrupted to a
    # letter) breaks that pattern's match entirely — not just the exact
    # comparison, but extraction itself returns nothing, which then starves
    # Pass 4's fuzzy matching of any candidate to compare against. Tried
    # last, after the stricter patterns, so it never overrides a cleaner
    # match — it only fires when nothing more specific was found.
    re.compile(r"\b[a-zA-Z0-9]{12,20}\b"),
]

_CONFUSION_MAP = str.maketrans({
    "O": "0", "o": "0",
    "I": "1", "l": "1",
    "S": "5", "s": "5",
})


def extract_utr_candidates(*texts: str | None) -> list[str]:
    """Pull every UTR-shaped substring out of one or more free-text fields
    (typically a bank narration and, separately, a ref_no column). Returns
    candidates in the order found, deduplicated, WITHOUT judging whether any
    of them are actually correct — that judgment belongs to Pass 1 (exact)
    or Pass 4 (fuzzy), not to extraction."""
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for pattern in _UTR_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(0)
                if candidate not in seen:
                    seen.append(candidate)
    return seen


def canonicalize(utr: str) -> str:
    """Collapse 0/O, 1/I/l, 5/S to single representatives, for confusion-
    aware comparison. NOT a general normalization — deliberately narrow to
    the specific confusions real OCR/transcription produces, per the
    roadmap's Part 3.4. Two UTRs that are canonically identical may still be
    genuinely different in the real world; this is a candidate-narrowing
    tool for Pass 4, always paired with an independent amount check before
    accepting a match — never sufficient on its own.
    """
    return utr.translate(_CONFUSION_MAP)

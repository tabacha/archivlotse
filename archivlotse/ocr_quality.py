from __future__ import annotations

import re

from wordfreq import zipf_frequency

_WORD_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ]{3,}$")

# How common (on the 0-8 Zipf scale) a token must be in `lang` to count as
# a recognized word. Calibrated against real documents: a plain ">0"
# threshold is far too lenient (wordfreq's corpus matches short garbage
# fragments by chance), while this value cleanly separated a known
# badly-garbled OCR document from ordinary correspondence in testing.
_MIN_ZIPF = 4.0


def ocr_quality_score(text: str, lang: str = "de") -> float:
    """Rough, dependency-light heuristic for OCR text quality: the
    fraction of plausible words (letters only, length >= 3) that are
    reasonably common `lang` vocabulary.

    This is a cheap first filter, not a replacement for a real language
    model - known limitations from calibration against real documents:
    - Numeric-/jargon-heavy documents (bank statements, technical
      certificates, forms) score lower even when perfectly recognized,
      since the metric only looks at alphabetic tokens.
    - It is NOT reliable for comparing "before vs. after OCR" on the same
      document - a correctly re-OCR'd technical document can score lower
      than its own garbled original if the clean version surfaces more
      jargon/proper nouns and less generic boilerplate text. Use it to
      gate whether the *current* text looks good enough, not to judge a
      replacement candidate against it.
    """
    tokens = [t for t in text.split() if _WORD_RE.match(t)]
    if not tokens:
        return 0.0
    known = sum(1 for t in tokens if zipf_frequency(t, lang) >= _MIN_ZIPF)
    return known / len(tokens)

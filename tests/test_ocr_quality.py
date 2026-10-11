from __future__ import annotations

from archivlotse.ocr_quality import ocr_quality_score

GOOD_TEXT = (
    "Sehr geehrte Damen und Herren, vielen Dank für Ihre Nachricht vom "
    "vergangenen Monat. Wir werden uns schnellstmöglich bei Ihnen melden "
    "und die gewünschten Unterlagen zusenden."
)

GARBLED_TEXT = "ne G ER eae SDI ine ag SE a BO I PER I ve ars ioe al DBSU se Eher SORES SES"


def test_clean_german_text_scores_high():
    assert ocr_quality_score(GOOD_TEXT) > 0.7


def test_garbled_text_scores_low():
    assert ocr_quality_score(GARBLED_TEXT) < 0.3


def test_empty_text_scores_zero():
    assert ocr_quality_score("") == 0.0
    assert ocr_quality_score("   ") == 0.0

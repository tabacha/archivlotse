from __future__ import annotations

import io

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from archivlotse.pdf_text import analyze_pdf


def _image_xobject() -> DecodedStreamObject:
    image = DecodedStreamObject()
    image.set_data(b"\x00")
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    return image


def make_pdf(*page_streams: bytes) -> bytes:
    """A PDF with one page per content stream; every page offers an image
    XObject /Im0 and a font /F1, so a stream can paint either or both."""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for stream in page_streams:
        page = writer.add_blank_page(width=200, height=200)
        content = DecodedStreamObject()
        content.set_data(stream)
        page[NameObject("/Contents")] = writer._add_object(content)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): writer._add_object(_image_xobject())}
                ),
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
                NameObject("/ProcSet"): ArrayObject([NameObject("/PDF")]),
            }
        )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


IMAGE = b"q 200 0 0 200 0 0 cm /Im0 Do Q\n"
VISIBLE_TEXT = b"BT /F1 12 Tf 10 10 Td (Rechnung) Tj ET\n"
INVISIBLE_TEXT = b"BT 3 Tr /F1 12 Tf 10 10 Td (Rechnung) Tj ET\n"


def test_image_only_page_is_a_scan():
    analysis = analyze_pdf(make_pdf(IMAGE))
    assert (analysis.scanned_pages, analysis.page_count) == (1, 1)
    assert analysis.needs_ocr


def test_invisible_ocr_text_layer_still_counts_as_scan():
    analysis = analyze_pdf(make_pdf(IMAGE + INVISIBLE_TEXT))
    assert analysis.needs_ocr


def test_visible_text_with_logo_image_is_not_a_scan():
    analysis = analyze_pdf(make_pdf(IMAGE + VISIBLE_TEXT))
    assert analysis.scanned_pages == 0
    assert not analysis.needs_ocr


def test_render_mode_is_restored_by_Q():
    # invisible mode set inside q...Q must not make later text invisible
    stream = IMAGE + b"q 3 Tr Q\n" + VISIBLE_TEXT
    assert not analyze_pdf(make_pdf(stream)).needs_ocr


def test_one_scanned_page_among_text_pages_needs_ocr():
    analysis = analyze_pdf(make_pdf(VISIBLE_TEXT, IMAGE, VISIBLE_TEXT))
    assert (analysis.scanned_pages, analysis.page_count) == (1, 3)
    assert analysis.needs_ocr


def test_blank_page_is_not_a_scan():
    assert not analyze_pdf(make_pdf(b"")).needs_ocr

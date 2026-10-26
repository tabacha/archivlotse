"""Decide whether a PDF is a scan (needs OCR) or already carries real text.

Deliberately structural, not a text-quality metric: a page counts as a
scanned page if it shows at least one image but no *visible* text. Text
drawn with render mode 3 ("invisible") doesn't count as visible - that's
exactly how OCR text layers are embedded over a scanned image (Paperless'
own OCRmyPDF, scanner apps, ...), so a scan stays a scan even if someone
already OCR'd it. A born-digital PDF (invoice from a web portal, letter
exported from Word) draws its text visibly and is never re-OCR'd.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.generic import ContentStream

# Operators that paint text / images in a PDF content stream.
_TEXT_SHOW_OPERATORS = {b"Tj", b"TJ", b"'", b'"'}
_INVISIBLE_RENDER_MODE = 3


@dataclass
class PageContent:
    has_visible_text: bool = False
    has_image: bool = False


@dataclass
class PdfAnalysis:
    page_count: int
    scanned_pages: int

    @property
    def needs_ocr(self) -> bool:
        return self.scanned_pages > 0


def _scan_content(
    reader: PdfReader, content, resources, result: PageContent, seen: set[int]
) -> None:
    """Walk one content stream (a page or a Form XObject) and record
    whether it paints visible text and/or an image. Render mode is part of
    the graphics state, so it is saved/restored with q/Q."""
    xobjects = {}
    if resources is not None and "/XObject" in resources:
        xobjects = resources["/XObject"].get_object()

    render_mode = 0
    stack: list[int] = []
    for operands, operator in content.operations:
        if operator == b"q":
            stack.append(render_mode)
        elif operator == b"Q":
            render_mode = stack.pop() if stack else 0
        elif operator == b"Tr" and operands:
            render_mode = int(operands[0])
        elif operator in _TEXT_SHOW_OPERATORS:
            if render_mode != _INVISIBLE_RENDER_MODE:
                result.has_visible_text = True
        elif operator == b"BI":  # inline image
            result.has_image = True
        elif operator == b"Do" and operands:
            xobject_ref = xobjects.get(operands[0])
            if xobject_ref is None:
                continue
            xobject = xobject_ref.get_object()
            subtype = xobject.get("/Subtype")
            if subtype == "/Image":
                result.has_image = True
            elif subtype == "/Form" and id(xobject) not in seen:
                seen.add(id(xobject))
                form_content = ContentStream(xobject, reader)
                _scan_content(reader, form_content, xobject.get("/Resources"), result, seen)
        if result.has_visible_text and result.has_image:
            return


def analyze_pdf(pdf_bytes: bytes) -> PdfAnalysis:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    scanned = 0
    for page in reader.pages:
        result = PageContent()
        content = page.get_contents()
        if content is not None:
            stream = ContentStream(content, reader)
            _scan_content(reader, stream, page.get("/Resources"), result, set())
        if result.has_image and not result.has_visible_text:
            scanned += 1
    return PdfAnalysis(page_count=len(reader.pages), scanned_pages=scanned)

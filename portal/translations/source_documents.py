"""Read the text out of a talk script or slide deck an event owner uploaded (#1172).

Scripts can be confidential, so a file is not kept after the request that uploads it. Its
only copy on disk is the temporary file the multipart parser uses for larger uploads.
"""

from __future__ import annotations

import io
import logging
import os
import time

from pypdf import PageObject, PdfReader

logger = logging.getLogger(__name__)

#: Slide decks carry images, so this is larger than the 2 MB allowed for a glossary CSV.
MAX_SOURCE_BYTES = 10 * 1024 * 1024
#: Multipart boundaries, headers and the form's other fields, on top of the file itself.
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
#: How much of an oversized upload is read and thrown away so its sender sees the error.
MAX_DISCARDED_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_SOURCE_CHARS = 1_000_000
MAX_PDF_PAGES = 500
#: Checked at every PDF drawing operator, so one crafted document cannot occupy a worker thread for long.
PDF_READ_SECONDS = 30.0
TOO_SLOW_MESSAGE = "This PDF took too long to read. Upload a smaller file."
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
PDF_SUFFIX = ".pdf"
UNSUPPORTED_TYPE_MESSAGE = "Upload a plain text (.txt), Markdown (.md) or PDF (.pdf) file."
TOO_LARGE_MESSAGE = f"Talk material must be {MAX_SOURCE_BYTES // (1024 * 1024)} MB or smaller."


class SourceDocumentError(ValueError):
    """The upload cannot be used; the message is written for the person who uploaded it."""


def extract_page_text(page: PageObject, deadline: float) -> str:
    """One page's text, stopping as soon as the page runs past the time or size limit."""
    page_chars = 0

    def check_deadline(_operator: object, _operands: object, _cm: object, _tm: object) -> None:
        if time.monotonic() > deadline:
            raise SourceDocumentError(TOO_SLOW_MESSAGE)

    def count_chars(text: str, _cm: object, _tm: object, _font: object, _size: object) -> None:
        nonlocal page_chars
        page_chars += len(text)
        if page_chars > MAX_SOURCE_CHARS:
            raise SourceDocumentError("This PDF has too much text on a single page.")

    return page.extract_text(visitor_operand_before=check_deadline, visitor_text=count_chars)


def extract_pdf_text(content: bytes) -> str:
    if b"%PDF-" not in content[:1024]:
        raise SourceDocumentError("This file is not a valid PDF.")
    deadline = time.monotonic() + PDF_READ_SECONDS
    pages: list[str] = []
    length = 0
    # pypdf raises plain built-in errors (KeyError, TypeError, ...) on damaged files, so no narrower
    # type covers them. Only the error's type is logged: its text can quote the document.
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise SourceDocumentError("This PDF is password-protected. Remove the password and upload it again.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise SourceDocumentError(f"This PDF has more than {MAX_PDF_PAGES} pages.")
        for page in reader.pages:
            if time.monotonic() > deadline:
                raise SourceDocumentError(TOO_SLOW_MESSAGE)
            page_text = extract_page_text(page, deadline)
            pages.append(page_text)
            length += len(page_text)
            if length >= MAX_SOURCE_CHARS:
                break
    except SourceDocumentError:
        raise
    except Exception as exc:
        logger.warning("Could not read an uploaded PDF: %s", type(exc).__name__)
        raise SourceDocumentError("This PDF could not be read. It may be damaged.") from exc
    return "\n".join(pages)


def decode_text_document(content: bytes) -> str:
    if b"\x00" in content:
        raise SourceDocumentError("This does not look like a text file. " + UNSUPPORTED_TYPE_MESSAGE)
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceDocumentError("Text files must use UTF-8 encoding.") from exc


def extract_source_text(filename: str, content: bytes) -> str:
    """The document's text, limited to ``MAX_SOURCE_CHARS``.

    Raises SourceDocumentError when the file is unsupported, unreadable or holds no text.
    """
    suffix = os.path.splitext(filename)[1].lower()
    if suffix == PDF_SUFFIX:
        text = extract_pdf_text(content)
    elif suffix in TEXT_SUFFIXES:
        text = decode_text_document(content)
    else:
        raise SourceDocumentError(UNSUPPORTED_TYPE_MESSAGE)
    if not text.strip():
        raise SourceDocumentError("No text was found in this file. Scanned PDFs are not supported.")
    return text[:MAX_SOURCE_CHARS]

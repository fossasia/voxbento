"""Read the text out of a talk script or slide deck an event owner uploaded (#1172).

Files are parsed in memory and never written anywhere: scripts can be confidential.
"""

from __future__ import annotations

import io
import logging
import os
import time

from pypdf import PdfReader

logger = logging.getLogger(__name__)

#: Slide decks carry images, so this is larger than the 2 MB allowed for a glossary CSV.
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_CHARS = 1_000_000
MAX_PDF_PAGES = 500
#: Checked between pages, so one crafted document cannot occupy a worker thread for long.
PDF_READ_SECONDS = 30.0
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
PDF_SUFFIX = ".pdf"
UNSUPPORTED_TYPE_MESSAGE = "Upload a plain text (.txt), Markdown (.md) or PDF (.pdf) file."
TOO_LARGE_MESSAGE = f"Talk material must be {MAX_SOURCE_BYTES // (1024 * 1024)} MB or smaller."


class SourceDocumentError(ValueError):
    """The upload cannot be used; the message is written for the person who uploaded it."""


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
                raise SourceDocumentError("This PDF took too long to read. Upload a smaller file.")
            page_text = page.extract_text()
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

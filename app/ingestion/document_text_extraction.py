# Shared file-content-to-text helpers used by any connector that reads
# regular files off a document store (Google Drive, SharePoint, ...).
# Factored out so PDF/DOCX parsing (and its size cap) isn't duplicated per
# connector type. Originally built for app/ingestion/google_drive_source.py.
from io import BytesIO
from typing import Callable

# Mime types read as plain text directly, with no parsing step.
PLAIN_TEXT_MIME_TYPES = {"text/plain", "text/markdown", "text/csv"}

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Legacy binary .doc (application/msword) is a much harder format to parse
# reliably and deliberately isn't supported, same "don't guess" spirit as
# everything else these connectors skip.


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(p.strip() for p in pages if p.strip())


def extract_docx_text(data: bytes) -> str:
    from docx import Document

    doc = Document(BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


# Files needing a local parsing step (not just a plain-text download) to get
# their text out. A scanned/image-only PDF has no text layer for pypdf to
# find; extract_pdf_text then returns "", and the caller treats that the
# same as any other empty result (skip), not an error.
BINARY_TEXT_PARSERS: dict[str, Callable[[bytes], str]] = {
    PDF_MIME: extract_pdf_text,
    DOCX_MIME: extract_docx_text,
}
# Keeps parsing cost/time bounded regardless of how large a shared file is.
MAX_BINARY_BYTES = 15 * 1024 * 1024

"""Turn uploaded training plans/results into Claude content blocks.

PDF  -> a base64 document block (Claude reads PDFs natively)
DOCX -> text extracted from word/document.xml (stdlib only)
TXT/MD/CSV -> text
Legacy .doc (binary Word) is not readable: callers get a clear error.
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from pathlib import Path
from typing import Any

MAX_BYTES = 10 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".csv"}
ALLOWED_EXT = TEXT_EXT | {".pdf", ".docx"}


class AttachmentError(ValueError):
    pass


def docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise AttachmentError("not a readable .docx file") from exc
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_block(name: str, data: bytes) -> dict[str, Any]:
    ext = Path(name).suffix.lower()
    if ext == ".doc":
        raise AttachmentError(f"{name}: old .doc files can't be read; save it as .docx or PDF")
    if ext not in ALLOWED_EXT:
        raise AttachmentError(f"{name}: only PDF, .docx, .txt, .md or .csv files")
    if len(data) > MAX_BYTES:
        raise AttachmentError(f"{name}: larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise AttachmentError(f"{name}: empty file")
    if ext == ".pdf":
        return {"type": "document", "title": name,
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.standard_b64encode(data).decode("ascii")}}
    text = docx_text(data) if ext == ".docx" else data.decode("utf-8-sig", errors="replace")
    return {"type": "text", "text": f'<attachment name="{name}">\n{text}\n</attachment>'}

"""Bounded text extraction shared by local Knowledge file abilities."""

from __future__ import annotations

import html
import io
import re
import zipfile


SUPPORTED_EXTENSIONS = {
    "pdf", "docx", "pptx", "xlsx", "txt", "md", "csv", "tsv", "json",
    "xml", "yaml", "yml", "html", "htm", "log",
}


def strip_markup(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def zip_xml_text(data: bytes, prefixes: tuple[str, ...]) -> str:
    chunks: list[str] = []
    expanded = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid Office document") from exc
    with archive:
        for info in archive.infolist()[:500]:
            if not info.filename.startswith(prefixes) or not info.filename.endswith(".xml"):
                continue
            expanded += info.file_size
            if expanded > 16 * 1024 * 1024:
                raise ValueError("compressed Office document expands beyond the extraction limit")
            raw = archive.read(info).decode("utf-8", errors="replace")
            chunks.extend(html.unescape(part) for part in re.findall(r">([^<>]+)<", raw) if part.strip())
    return " ".join(chunks)


def extract_text(data: bytes, name: str, content_type: str = "") -> str:
    lower = str(name or "").lower()
    ctype = str(content_type or "").lower()
    if lower.endswith((".html", ".htm")) or "text/html" in ctype:
        return strip_markup(data.decode("utf-8", errors="replace"))
    if lower.endswith(".docx"):
        return zip_xml_text(data, ("word/",))
    if lower.endswith(".pptx"):
        return zip_xml_text(data, ("ppt/slides/", "ppt/notesSlides/"))
    if lower.endswith(".xlsx"):
        return zip_xml_text(data, ("xl/sharedStrings", "xl/worksheets/"))
    if lower.endswith(".pdf") or "application/pdf" in ctype:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF text extraction requires pypdf") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:100])
        except Exception as exc:
            raise ValueError("invalid or unreadable PDF") from exc
    text_extensions = SUPPORTED_EXTENSIONS - {"pdf", "docx", "pptx", "xlsx"}
    if ctype.startswith("text/") or any(lower.endswith(f".{ext}") for ext in text_extensions):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"text extraction is not available for {content_type or name}")

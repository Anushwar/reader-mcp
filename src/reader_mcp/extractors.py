"""Turn a file into text units (pages, sections or row blocks) plus document properties.

Parsing untrusted files is risky, so the server never calls `extract` directly: it runs
`python -m reader_mcp.extractors <file> <filename>` in a child process with a timeout
(see `extract_isolated`). A malformed or hostile file can hang or crash only that child.

Adding a format (or OCR later) means adding one function and one line in `EXTRACTORS`.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

EXTRACTOR_VERSION = 4  # bump when extraction output changes so stored documents get re-extracted
CHUNK_CHARS = 4000  # size of a "section" for formats without real pages
# Bounds on extracted text, so one huge file can't exhaust memory: ~10,000 pages, far more than
# any agent will read, while the original file itself may still be up to READER_MAX_FILE_MB.
MAX_TEXT_CHARS = 20_000_000
MAX_TEXT_FILE_BYTES = 32 * 1024 * 1024
CSV_ROWS_PER_UNIT = 200
MAX_UNZIPPED_BYTES = 300 * 1024 * 1024  # zip-bomb guard for .docx
OCR_MIN_CHARS_PER_PAGE = 25
LINE_BREAK_HYPHEN = re.compile(r"(?<=\w)-\s*\n\s*(?=[a-z])")

IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"II*\x00", b"MM\x00*", b"RIFF")
WEB_PAGE = "This is a web page (HTML), not a document. reader handles PDF, DOCX and text files."


class UnsupportedFile(ValueError):
    """The file type is not supported or the file cannot be read."""


class ExtractionError(RuntimeError):
    """Extraction failed, crashed or timed out."""


# --------------------------------------------------------------------------- helpers


def normalize_text(text: str) -> str:
    """NFKC turns typographic ligatures ("ﬁ" -> "fi") and other compatibility characters into
    plain text. Without it, PDFs typeset with ligatures are unsearchable for words like
    "classification". Soft hyphens are invisible line-break hints, so drop them."""
    return unicodedata.normalize("NFKC", text).replace("\u00ad", "")


def _decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def _read_text_file(path: Path) -> tuple[bytes, bool]:
    """Read at most MAX_TEXT_FILE_BYTES, cut at a line break so no character is split."""
    with path.open("rb") as file:
        data = file.read(MAX_TEXT_FILE_BYTES + 1)
    if len(data) <= MAX_TEXT_FILE_BYTES:
        return data, False
    data = data[:MAX_TEXT_FILE_BYTES]
    return data[: data.rfind(b"\n") + 1] or data, True


def _truncation_note(truncated: bool) -> dict[str, Any]:
    return {"truncated": True, "truncated_note": "Text extraction stopped at the size limit; later content "
            "isn't searchable."} if truncated else {}


def _chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split text into ~size-char chunks, preferring line breaks as boundaries."""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut > start + size // 2:
                end = cut + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c] or [""]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip() or None


def _clean(props: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in props.items() if v not in (None, "", [], {})}


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


# --------------------------------------------------------------------------- formats


def _pdf_properties(path: Path) -> dict[str, Any]:
    """Document info via pypdf, which parses PDF dates; the text itself comes from pdfium.

    Best effort: a PDF pypdf can't parse may still be readable by pdfium, so don't fail on it."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
    except Exception:
        return {}
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise UnsupportedFile("PDF is password-protected")
    info: dict[str, Any] = {}
    try:
        meta = reader.metadata
        if meta:
            info = {key: _iso(getattr(meta, key)) for key in ("title", "author", "subject", "creator", "producer")}
            for key, attr in (("created", "creation_date"), ("modified", "modification_date")):
                try:
                    info[key] = _iso(getattr(meta, attr))
                except Exception:
                    pass
    except Exception:
        pass
    return _clean(info)


def _pdfium_page_text(document: Any, index: int) -> str:
    """One page's text; a corrupt page becomes empty instead of failing the whole document."""
    try:
        page = document[index]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            finally:
                textpage.close()
        finally:
            page.close()
    except Exception:
        return ""
    # pdfium marks a hyphen it removed at a line break with U+FFFE; drop it to re-join the word.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("￾", "")
    return LINE_BREAK_HYPHEN.sub("", text).strip()


def extract_pdf(path: Path, filename: str) -> dict[str, Any]:
    import pypdfium2 as pdfium

    properties = _pdf_properties(path)
    pages: list[str] = []
    used, extracted = 0, 0
    document = pdfium.PdfDocument(path)  # Chrome's PDF engine: ~25x faster than pure-Python parsing
    try:
        for index in range(len(document)):
            if used >= MAX_TEXT_CHARS:
                pages.append("")  # keep page numbering, skip the work
                continue
            text = _pdfium_page_text(document, index)[: MAX_TEXT_CHARS - used]
            pages.append(text)
            used += len(text)
            extracted += 1
    finally:
        document.close()

    empty = [i + 1 for i, text in enumerate(pages[:extracted]) if len(text) < OCR_MIN_CHARS_PER_PAGE]
    needs_ocr = bool(pages) and len(empty) == len(pages)
    extra: dict[str, Any] = _truncation_note(extracted < len(pages) or used >= MAX_TEXT_CHARS)
    if empty and not needs_ocr:
        extra["pages_without_text"] = empty[:50]
    return {
        "unit": "page",
        "pages": pages,
        "properties": properties,
        "needs_ocr": needs_ocr,
        "ocr_reason": "No embedded text found; likely a scanned or image-only PDF." if needs_ocr else None,
        "extra": extra,
    }


def extract_docx(path: Path, filename: str) -> dict[str, Any]:
    from docx import Document
    from docx.table import Table

    with zipfile.ZipFile(path) as zf:
        if sum(info.file_size for info in zf.infolist()) > MAX_UNZIPPED_BYTES:
            raise UnsupportedFile("DOCX expands to an unreasonable size (possible zip bomb)")

    doc = Document(str(path))
    blocks: list[str] = []
    headings: list[str] = []
    tables = 0
    for item in doc.iter_inner_content():
        if isinstance(item, Table):
            tables += 1
            rows = [[_md_cell(cell.text) for cell in row.cells] for row in item.rows]
            if rows:
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * len(rows[0])]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                blocks.append("\n".join(lines))
            continue
        text = item.text.strip()
        if not text:
            continue
        style = (item.style.name if item.style is not None else "") or ""
        if style.startswith("Heading") or style == "Title":
            level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
            headings.append(text)
            text = "#" * min(level, 6) + " " + text
        blocks.append(text)

    core = doc.core_properties
    props = {
        "title": _iso(core.title),
        "author": _iso(core.author),
        "subject": _iso(core.subject),
        "keywords": _iso(core.keywords),
        "last_modified_by": _iso(core.last_modified_by),
        "created": _iso(core.created),
        "modified": _iso(core.modified),
    }
    text = "\n\n".join(blocks)
    return {
        "unit": "section",
        "pages": _chunk(text[:MAX_TEXT_CHARS]),
        "properties": _clean(props),
        "needs_ocr": False,
        "ocr_reason": None,
        "extra": _clean({"headings": headings[:100], "table_count": tables}) | _truncation_note(len(text) > MAX_TEXT_CHARS),
    }


def _column_type(values: list[str]) -> str:
    filled = [v.strip() for v in values if v.strip()]
    if not filled:
        return "empty"
    try:
        for v in filled:
            float(v.replace(",", ""))
        return "number"
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            for v in filled:
                datetime.strptime(v, fmt)
            return "date"
        except ValueError:
            continue
    return "text"


def extract_table(path: Path, filename: str) -> dict[str, Any]:
    data, truncated = _read_text_file(path)
    text, encoding = _decode(data)
    csv.field_size_limit(10 * 1024 * 1024)
    if filename.lower().endswith(".tsv"):
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:16384], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if not rows:
        return {"unit": "rows", "pages": [""], "properties": {}, "needs_ocr": False, "ocr_reason": None,
                "extra": {"row_count": 0, "columns": []}}

    header, body = rows[0], rows[1:]
    head_md = ["| " + " | ".join(_md_cell(h) for h in header) + " |", "|" + " --- |" * len(header)]
    pages = []
    for start in range(0, max(len(body), 1), CSV_ROWS_PER_UNIT):
        block = body[start : start + CSV_ROWS_PER_UNIT]
        pages.append("\n".join(head_md + ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in block]))

    columns = []
    for i, name in enumerate(header[:100]):
        values = [r[i] if i < len(r) else "" for r in body]
        columns.append({
            "name": name.strip() or f"column_{i + 1}",
            "type": _column_type(values[:5000]),
            "non_empty": sum(1 for v in values if v.strip()),
        })
    return {
        "unit": "rows",
        "pages": pages,
        "properties": {},
        "needs_ocr": False,
        "ocr_reason": None,
        "extra": {"row_count": len(body), "column_count": len(header), "columns": columns,
                  "delimiter": delimiter, "encoding": encoding, "rows_per_unit": CSV_ROWS_PER_UNIT}
                 | _truncation_note(truncated),
    }


def extract_text(path: Path, filename: str) -> dict[str, Any]:
    data, truncated = _read_text_file(path)
    text, encoding = _decode(data)
    props: dict[str, Any] = {}
    extra: dict[str, Any] = {"encoding": encoding, "line_count": text.count("\n") + 1} | _truncation_note(truncated)
    if filename.lower().endswith((".md", ".markdown")):
        title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), None)
        props = _clean({"title": title})
    if filename.lower().endswith(".json") and not truncated:
        try:
            json.loads(text)
            extra["valid_json"] = True
        except ValueError:
            extra["valid_json"] = False
    return {"unit": "section", "pages": _chunk(text), "properties": props, "needs_ocr": False,
            "ocr_reason": None, "extra": extra}


EXTRACTORS: dict[str, Callable[[Path, str], dict[str, Any]]] = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "csv": extract_table,
    "tsv": extract_table,
    "text": extract_text,
}


# --------------------------------------------------------------------------- dispatch


def detect_type(path: Path, filename: str) -> str:
    """Detect the format from content first (magic bytes), then the extension."""
    with path.open("rb") as file:
        head = file.read(8192)
    ext = Path(filename).suffix.lower().lstrip(".")
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                if "word/document.xml" in zf.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
        raise UnsupportedFile("ZIP-based file that is not a .docx (xlsx/pptx are not supported yet)")
    if head.startswith(IMAGE_MAGIC):
        raise UnsupportedFile("Images need OCR, which is not supported yet")
    if b"\x00" in head:
        raise UnsupportedFile("Binary file type is not supported")
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").lower().startswith((b"<!doctype html", b"<html")):
        raise UnsupportedFile(WEB_PAGE)
    if ext in ("csv", "tsv"):
        return ext
    return "text"


def extract(path: Path, filename: str) -> dict[str, Any]:
    kind = detect_type(path, filename)
    result = EXTRACTORS[kind](path, filename)
    pages, used = [], 0
    for page in result["pages"]:  # final safety net on total text, whatever the format
        page = normalize_text(page)[: MAX_TEXT_CHARS - used]
        pages.append(page)
        used += len(page)
    if used >= MAX_TEXT_CHARS:
        result["extra"] |= _truncation_note(True)
    result["pages"] = pages
    if kind == "text":
        ext = Path(filename).suffix.lower().lstrip(".")
        kind = {"markdown": "md"}.get(ext, ext) if ext in ("md", "markdown", "json") else "txt"
    result["type"] = kind
    return result


async def extract_isolated(path: Path, filename: str, timeout: float) -> dict[str, Any]:
    """Run `extract` in a child Python process so bad files can't hang or crash the server."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "reader_mcp.extractors", str(path), filename,
        # `-m` puts the working directory on sys.path; the document's own folder holds no Python
        # modules, so nothing there can be imported by mistake. (Not `-I`: that also drops
        # PYTHONPATH and user site-packages, which some installs rely on.)
        cwd=path.parent,
        stdin=asyncio.subprocess.DEVNULL,  # never share the MCP stdio pipe with the child
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ExtractionError(f"Extraction timed out after {timeout:g}s (READER_EXTRACT_TIMEOUT)") from None
    try:
        payload = json.loads(out)
    except ValueError:
        reason = err.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise ExtractionError(f"Extractor crashed while parsing this file ({reason[0][:200]})") from None
    if not payload.get("ok"):
        raise UnsupportedFile(payload.get("error") or "Could not read this file")
    return payload["result"]


def _main() -> None:
    path, filename = sys.argv[1], sys.argv[2]
    try:
        payload = {"ok": True, "result": extract(Path(path), filename)}
    except UnsupportedFile as exc:
        payload = {"ok": False, "error": str(exc)}
    except Exception as exc:  # parser bugs, corrupt files, ...
        payload = {"ok": False, "error": f"Could not parse file ({type(exc).__name__})"}
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    _main()

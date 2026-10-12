"""On-disk document library: <data_dir>/docs/<doc_id>/{original, meta.json, pages.json}.

doc_id is derived from the file's SHA-256, so ingesting the same bytes twice is a no-op.
Metadata and page text live in separate files so listing documents never loads their text.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .extractors import EXTRACTOR_VERSION, extract_isolated, normalize_text
from .sources import Incoming

DOC_ID = re.compile(r"^[a-f0-9]{32}$")  # first 128 bits of the SHA-256: collisions aren't practical
WORD = re.compile(r"\w+", re.UNICODE)
PREVIEW_CHARS = 600
SNIPPET_CHARS = 280
CACHED_DOCS = 16
CACHED_CHARS = 60_000_000  # the page cache is bounded by size as well as count


class NotFound(LookupError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.docs = settings.data_dir / "docs"
        self.uploads = settings.data_dir / "uploads"
        self.tmp = settings.data_dir / "tmp"
        for folder in (settings.data_dir, self.docs, self.uploads, self.tmp):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._pages: OrderedDict[str, tuple[int, list[str]]] = OrderedDict()  # small LRU of page text

    # ----------------------------------------------------------------- write

    async def ingest(self, incoming: Incoming) -> dict[str, Any]:
        doc_id = incoming.sha256[:32]
        folder = self.docs / doc_id
        if self.has(doc_id):
            existing = self.metadata(doc_id)
            if existing.get("extractor_version") == EXTRACTOR_VERSION and existing.get("sha256") == incoming.sha256:
                incoming.path.unlink(missing_ok=True)
                os.utime(folder)  # restart the retention clock
                return {**existing, "already_ingested": True}

        folder.mkdir(mode=0o700, exist_ok=True)
        os.utime(folder)  # a re-extraction of an old document must not look expired mid-way
        original = folder / "original"
        os.replace(incoming.path, original)
        try:
            result = await extract_isolated(original, incoming.filename, self.settings.extract_timeout)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        pages: list[str] = result["pages"]
        preview = "\n".join(pages[:5])[:PREVIEW_CHARS]
        metadata = {
            "doc_id": doc_id,
            "filename": incoming.filename,
            "type": result["type"],
            "source": incoming.source,
            "size_bytes": incoming.size,
            "sha256": incoming.sha256,
            "unit": result["unit"],
            "unit_count": len(pages),
            "char_count": sum(map(len, pages)),
            "word_count": sum(sum(1 for _ in WORD.finditer(page)) for page in pages),  # no giant word list
            "properties": result["properties"],
            "needs_ocr": result["needs_ocr"],
            **({"ocr_reason": result["ocr_reason"]} if result["ocr_reason"] else {}),
            **result["extra"],
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "extractor_version": EXTRACTOR_VERSION,
        }
        _write_json(folder / "pages.json", pages)
        _write_json(folder / "meta.json", metadata)  # written last: its presence marks the document as ready
        return {**metadata, "preview": preview}

    def delete(self, doc_id: str) -> None:
        shutil.rmtree(self._folder(doc_id))
        self._pages.pop(doc_id, None)

    def purge_expired(self) -> None:
        """Delete documents older than READER_RETENTION_HOURS, stale uploads and abandoned temp files."""
        now = time.time()
        if self.settings.retention_hours > 0:
            cutoff = now - self.settings.retention_hours * 3600
            for folder in self.docs.iterdir():
                if _older_than(folder, cutoff) and folder.is_dir():
                    shutil.rmtree(folder, ignore_errors=True)
                    self._pages.pop(folder.name, None)
        for folder, max_age in ((self.uploads, 2 * self.settings.upload_ttl), (self.tmp, 24 * 3600)):
            for file in folder.iterdir():
                if _older_than(file, now - max_age):
                    file.unlink(missing_ok=True)

    # ----------------------------------------------------------------- read

    def has(self, doc_id: str) -> bool:
        return bool(DOC_ID.match(doc_id or "")) and (self.docs / doc_id / "meta.json").exists()

    def _folder(self, doc_id: str) -> Path:
        if not self.has(doc_id):
            raise NotFound(f"Unknown doc_id: {doc_id}. Call list_documents to see what's available.")
        return self.docs / doc_id

    def metadata(self, doc_id: str) -> dict[str, Any]:
        return json.loads((self._folder(doc_id) / "meta.json").read_text())

    def pages(self, doc_id: str) -> list[str]:
        path = self._folder(doc_id) / "pages.json"
        stamp = path.stat().st_mtime_ns
        cached = self._pages.get(doc_id)
        if cached and cached[0] == stamp:
            self._pages.move_to_end(doc_id)
            return cached[1]
        pages = json.loads(path.read_text())
        self._pages[doc_id] = (stamp, pages)
        size = lambda: sum(sum(map(len, p)) for _, p in self._pages.values())  # noqa: E731
        while len(self._pages) > 1 and (len(self._pages) > CACHED_DOCS or size() > CACHED_CHARS):
            self._pages.popitem(last=False)
        return pages

    def list(self) -> list[dict[str, Any]]:
        items = []
        for folder in self.docs.iterdir():
            path = folder / "meta.json"
            if path.exists():
                meta = json.loads(path.read_text())
                items.append({k: meta.get(k) for k in
                              ("doc_id", "filename", "type", "unit", "unit_count", "word_count",
                               "needs_ocr", "ingested_at")} | {"title": meta["properties"].get("title")})
        return sorted(items, key=lambda m: m["ingested_at"], reverse=True)

    def read(self, doc_id: str, start: int, end: int | None, max_chars: int) -> dict[str, Any]:
        meta, pages = self.metadata(doc_id), self.pages(doc_id)
        total = len(pages)
        if not 1 <= start <= total:
            raise ValueError(f"start must be between 1 and {total}.")
        end = total if end is None else max(start, min(end, total))

        out, used, next_start = [], 0, None
        for n in range(start, end + 1):
            text = pages[n - 1]
            if out and used + len(text) > max_chars:
                next_start = n
                break
            item: dict[str, Any] = {meta["unit"]: n, "text": text[:max_chars]}
            if len(text) > max_chars:
                item["truncated"] = True
            out.append(item)
            used += len(item["text"])
        if next_start is None and end < total:
            next_start = end + 1
        return {"doc_id": doc_id, "filename": meta["filename"], "unit": meta["unit"], "unit_count": total,
                "items": out, "next_start": next_start}

    def search(self, query: str, doc_id: str | None, limit: int) -> list[dict[str, Any]]:
        query = normalize_text(query)
        terms = [t for t in dict.fromkeys(WORD.findall(query.lower())) if len(t) > 1]
        if not terms:
            raise ValueError("Query must contain at least one word.")
        doc_ids = [doc_id] if doc_id else [m["doc_id"] for m in self.list()]
        units = []  # (doc_id, filename, unit, n, text, counts)
        for did in doc_ids:
            meta = self.metadata(did)
            for n, text in enumerate(self.pages(did), 1):
                units.append((did, meta["filename"], meta["unit"], n, text, Counter(WORD.findall(text.lower()))))
        if not units:
            return []

        df = {t: sum(1 for u in units if u[5][t]) for t in terms}
        phrase = query.lower().strip()
        hits = []
        for did, filename, unit, n, text, counts in units:
            score = sum((1 + math.log(counts[t])) * math.log(1 + len(units) / df[t]) for t in terms if counts[t])
            if not score:
                continue
            lowered = text.lower()
            if len(terms) > 1 and phrase in lowered:
                score *= 2
            pos = lowered.find(phrase) if phrase in lowered else min(lowered.find(t) for t in terms if counts[t])
            lo = max(0, pos - SNIPPET_CHARS // 3)
            snippet = text[lo : lo + SNIPPET_CHARS].replace("\n", " ").strip()
            hits.append({"doc_id": did, "filename": filename, unit: n, "score": round(score, 3),
                         "snippet": ("…" if lo else "") + snippet + ("…" if lo + SNIPPET_CHARS < len(text) else "")})
        return sorted(hits, key=lambda h: h["score"], reverse=True)[:limit]


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:  # removed by a concurrent ingest or upload meanwhile
        return False


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)

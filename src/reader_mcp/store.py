"""On-disk document library: <data_dir>/docs/<doc_id>/{original, doc.json}.

doc_id is derived from the file's SHA-256, so ingesting the same bytes twice is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .extractors import EXTRACTOR_VERSION, extract_isolated, normalize_text
from .sources import Incoming

DOC_ID = re.compile(r"^[a-f0-9]{16}$")
WORD = re.compile(r"\w+", re.UNICODE)
PREVIEW_CHARS = 600
SNIPPET_CHARS = 280


class NotFound(LookupError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.docs = settings.data_dir / "docs"
        self.uploads = settings.data_dir / "uploads"
        for folder in (settings.data_dir, self.docs, self.uploads):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    # ----------------------------------------------------------------- write

    async def ingest(self, incoming: Incoming) -> dict[str, Any]:
        self.purge_expired()
        digest = hashlib.sha256(incoming.data).hexdigest()
        doc_id = digest[:16]
        folder = self.docs / doc_id
        if (folder / "doc.json").exists():
            existing = self.metadata(doc_id)
            if existing.get("extractor_version") == EXTRACTOR_VERSION:
                os.utime(folder)  # restart the retention clock
                return {**existing, "already_ingested": True}

        folder.mkdir(mode=0o700, exist_ok=True)
        original = folder / "original"
        original.write_bytes(incoming.data)
        try:
            result = await extract_isolated(original, incoming.filename, self.settings.extract_timeout)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        pages: list[str] = result["pages"]
        full_text = "\n".join(pages)
        metadata = {
            "doc_id": doc_id,
            "filename": incoming.filename,
            "type": result["type"],
            "source": incoming.source,
            "size_bytes": len(incoming.data),
            "sha256": digest,
            "unit": result["unit"],
            "unit_count": len(pages),
            "char_count": len(full_text),
            "word_count": len(WORD.findall(full_text)),
            "properties": result["properties"],
            "needs_ocr": result["needs_ocr"],
            **({"ocr_reason": result["ocr_reason"]} if result["ocr_reason"] else {}),
            **result["extra"],
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "extractor_version": EXTRACTOR_VERSION,
        }
        tmp = folder / "doc.json.tmp"
        tmp.write_text(json.dumps({"metadata": metadata, "pages": pages}))
        tmp.replace(folder / "doc.json")
        return {**metadata, "preview": full_text[:PREVIEW_CHARS]}

    def delete(self, doc_id: str) -> bool:
        folder = self._folder(doc_id)
        shutil.rmtree(folder)
        return True

    def purge_expired(self) -> None:
        """Delete documents older than READER_RETENTION_HOURS and stale uploads."""
        now = time.time()
        if self.settings.retention_hours > 0:
            cutoff = now - self.settings.retention_hours * 3600
            for folder in self.docs.iterdir():
                if folder.is_dir() and folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
        upload_cutoff = now - 2 * self.settings.upload_ttl
        for file in self.uploads.iterdir():
            if file.stat().st_mtime < upload_cutoff:
                file.unlink(missing_ok=True)

    # ----------------------------------------------------------------- read

    def _folder(self, doc_id: str) -> Path:
        if not DOC_ID.match(doc_id or "") or not (self.docs / doc_id / "doc.json").exists():
            raise NotFound(f"Unknown doc_id: {doc_id}. Call list_documents to see what's available.")
        return self.docs / doc_id

    def _load(self, doc_id: str) -> dict[str, Any]:
        return json.loads((self._folder(doc_id) / "doc.json").read_text())

    def metadata(self, doc_id: str) -> dict[str, Any]:
        return self._load(doc_id)["metadata"]

    def list(self) -> list[dict[str, Any]]:
        items = []
        for folder in self.docs.iterdir():
            path = folder / "doc.json"
            if path.exists():
                meta = json.loads(path.read_text())["metadata"]
                items.append({k: meta.get(k) for k in
                              ("doc_id", "filename", "type", "unit", "unit_count", "word_count",
                               "needs_ocr", "ingested_at")} | {"title": meta["properties"].get("title")})
        return sorted(items, key=lambda m: m["ingested_at"], reverse=True)

    def read(self, doc_id: str, start: int, end: int | None, max_chars: int) -> dict[str, Any]:
        doc = self._load(doc_id)
        pages, meta = doc["pages"], doc["metadata"]
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
            doc = self._load(did)
            meta = doc["metadata"]
            for n, text in enumerate(doc["pages"], 1):
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

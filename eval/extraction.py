"""Extraction quality: compare reader's text with Poppler's pdftotext, page by page.

    uv run python eval/extraction.py path/to/book.pdf

Requires `pdftotext` (brew install poppler / apt install poppler-utils).
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import anyio

from common import call, load_key, reader


def words(text: str) -> list[str]:
    return re.sub(r"\s+", " ", text).strip().lower().split()


def poppler_page(pdf: Path, n: int) -> str:
    return subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), str(pdf), "-"],
                          capture_output=True, text=True, check=True).stdout


async def main(pdf: Path) -> None:
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found (install poppler)")
    with tempfile.TemporaryDirectory() as tmp:
        async with reader(Path(tmp), [pdf.parent]) as client:
            started = time.time()
            meta = await call(client, "ingest_document", path=str(pdf))
            print(f"ingest: {time.time() - started:.1f}s, {meta['unit_count']} pages, {meta['word_count']} words, "
                  f"needs_ocr={meta['needs_ocr']}")
            pages, start = [], 1
            while start:
                chunk = await call(client, "read_document", doc_id=meta["doc_id"], start=start, max_chars=100_000)
                pages += [item["text"] for item in chunk["items"]]
                start = chunk["next_start"]

            reference = [poppler_page(pdf, n) for n in range(1, len(pages) + 1)]
            scores = sorted(
                (difflib.SequenceMatcher(None, words(ours), words(ref), autojunk=False).ratio(), n)
                for n, (ours, ref) in enumerate(zip(pages, reference), 1)
            )
            ratios = [s for s, _ in scores]
            print(f"similarity to pdftotext: median {statistics.median(ratios):.3f}, mean {statistics.mean(ratios):.3f}, "
                  f"{sum(r >= 0.95 for r in ratios)}/{len(ratios)} pages >= 0.95")
            print("lowest pages (usually figures):", ", ".join(f"p{n}={s:.2f}" for s, n in scores[:5]))

            print("search recall per word [pages found / pages containing the word in pdftotext]:")
            for word in load_key()["recall_words"]:
                truth = {n for n, text in enumerate(reference, 1) if re.search(rf"\b{word}\b", text.lower())}
                found = await call(client, "search_documents", query=word, doc_id=meta["doc_id"], limit=50)
                hit = {r["page"] for r in found["results"]} & truth
                cap = " (capped at 50 results)" if len(truth) > 50 else ""
                print(f"  {word:15} {len(hit):>3}/{len(truth)}{cap}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    anyio.run(main, parser.parse_args().pdf.expanduser().resolve())

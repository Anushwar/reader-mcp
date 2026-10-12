"""Search quality: does search_documents put the answer's page at the top? No LLM involved.

    uv run python eval/search.py path/to/book.pdf [--shuffle]
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any

import anyio

from common import call, load_key, reader, remap, shuffle_pdf


async def score(client: Any, doc_id: str, items: list[dict[str, Any]], field: str) -> str:
    ranks = []
    for item in items:
        found = await call(client, "search_documents", query=item[field], doc_id=doc_id, limit=5)
        pages = [r["page"] for r in found["results"]]
        ranks.append(next((i + 1 for i, p in enumerate(pages) if p in item["gold"]), None))
    n = len(ranks)
    hit = lambda k: sum(1 for r in ranks if r and r <= k)  # noqa: E731
    mrr = sum(1 / r for r in ranks if r) / n
    return f"top-1 {hit(1)}/{n}, top-3 {hit(3)}/{n}, top-5 {hit(5)}/{n}, MRR {mrr:.2f}"


async def main(pdf: Path, shuffle: bool) -> None:
    key = load_key()
    with tempfile.TemporaryDirectory() as tmp:
        mapping = None
        if shuffle:
            pdf, mapping = Path(tmp) / "document.pdf", shuffle_pdf(pdf, Path(tmp) / "document.pdf")
        async with reader(Path(tmp) / "data", [pdf.parent]) as client:
            doc_id = (await call(client, "ingest_document", path=str(pdf)))["doc_id"]
            questions = remap(key["questions"], mapping)
            print(f"keyword queries     {await score(client, doc_id, questions, 'kw')}")
            print(f"full questions      {await score(client, doc_id, questions, 'q')}")
            print(f"paraphrased queries {await score(client, doc_id, remap(key['paraphrases'], mapping), 'query')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--shuffle", action="store_true", help="shuffle the PDF's pages first")
    args = parser.parse_args()
    anyio.run(main, args.pdf.expanduser().resolve(), args.shuffle)

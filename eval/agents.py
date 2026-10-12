"""End to end: headless Claude Code answering questions with reader vs. its built-in PDF reading.

    uv run python eval/agents.py path/to/book.pdf [--shuffle] [--ids 1,3,5,8,9,12,13]

Requires the `claude` CLI. Every question runs once per setup and is billed to your account
(about $0.50 for the reader runs; the built-in runs can cost $8+ on a shuffled book).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anyio

from common import call, load_key, reader, remap, server_config, shuffle_pdf

PROMPT = ("Answer using the PDF at {pdf}. Question: {question} "
          "Reply in at most two sentences and cite the PDF page number(s) you used.")
SETUPS = {
    "reader": ["--tools", "ToolSearch", "--strict-mcp-config", "--allowedTools", "mcp__reader"],
    "built-in": ["--tools", "Read", "--strict-mcp-config", "--allowedTools", "Read"],
}


def ask(setup: str, question: dict[str, Any], pdf: Path, workdir: Path, mcp_config: Path, budget: str) -> dict[str, Any]:
    cmd = ["claude", "-p", PROMPT.format(pdf=pdf, question=question["q"]), "--output-format", "json",
           "--no-session-persistence", "--max-budget-usd", budget, *SETUPS[setup]]
    if setup == "reader":
        cmd += ["--mcp-config", str(mcp_config)]
    out = subprocess.run(cmd, cwd=workdir, stdin=subprocess.DEVNULL, capture_output=True, text=True).stdout
    data = json.loads(out or "{}")
    text = data.get("result") or ""
    found = [a.lower() in text.lower() for a in question["answer"]]
    cited = {int(n) for n in re.findall(r"\b(\d{1,3})\b", text)}
    usage = data.get("usage", {})
    return {
        "correct": all(found) if question.get("all") else any(found),
        "page": bool(cited & set(question["gold"])),
        "input_tokens": sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens")),
        "seconds": data.get("duration_ms", 0) / 1000,
        "cost": data.get("total_cost_usd", 0.0),
        "answer": text,
    }


async def main(pdf: Path, shuffle: bool, ids: list[int], budget: str) -> None:
    key = load_key()
    with tempfile.TemporaryDirectory() as tmp:
        work, mapping = Path(tmp), None
        if shuffle:
            pdf, mapping = work / "document.pdf", shuffle_pdf(pdf, work / "document.pdf")
        questions = [q for q in remap(key["questions"], mapping) if q["id"] in ids]

        # Ingest once up front: reader's cost is per document, not per question.
        async with reader(work / "data", [pdf.parent]) as client:
            started = time.time()
            await call(client, "ingest_document", path=str(pdf))
            print(f"one-time ingest: {time.time() - started:.1f}s")
        mcp_config = work / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"reader": server_config(work / "data", [pdf.parent])}}))

        jobs = [(setup, q) for setup in SETUPS for q in questions]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda job: ask(*job, pdf, work, mcp_config, budget), jobs))

    print(f"\n{'':22}" + "".join(f"{s:>14}" for s in SETUPS))
    by_setup = {s: [r for (setup, _), r in zip(jobs, results) if setup == s] for s in SETUPS}
    rows = [
        ("correct answer", lambda rs: f"{sum(r['correct'] for r in rs)}/{len(rs)}"),
        ("correct page cited", lambda rs: f"{sum(r['page'] for r in rs)}/{len(rs)}"),
        ("median input tokens", lambda rs: f"{statistics.median(r['input_tokens'] for r in rs):,.0f}"),
        ("median seconds", lambda rs: f"{statistics.median(r['seconds'] for r in rs):.1f}"),
        ("total cost (USD)", lambda rs: f"{sum(r['cost'] for r in rs):.2f}"),
    ]
    for label, fmt in rows:
        print(f"{label:22}" + "".join(f"{fmt(by_setup[s]):>14}" for s in SETUPS))
    for (setup, q), r in zip(jobs, results):
        if not (r["correct"] and r["page"]):
            print(f"\n[{setup}] Q{q['id']} needs review: {r['answer'][:300]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--shuffle", action="store_true", help="shuffle pages to simulate an unfamiliar document")
    parser.add_argument("--ids", default="1,3,5,8,9,12,13", help="question ids to run")
    parser.add_argument("--budget", default="2", help="max USD per headless session")
    args = parser.parse_args()
    anyio.run(main, args.pdf.expanduser().resolve(), args.shuffle, [int(i) for i in args.ids.split(",")], args.budget)

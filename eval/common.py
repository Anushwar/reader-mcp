"""Helpers shared by the evaluation scripts."""

from __future__ import annotations

import json
import os
import random
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pypdf import PdfReader, PdfWriter

KEY = Path(__file__).with_name("questions.json")


def load_key(path: Path = KEY) -> dict[str, Any]:
    return json.loads(path.read_text())


def shuffle_pdf(src: Path, dst: Path, seed: int = 7) -> dict[int, int]:
    """Write a page-shuffled copy of src and return {original page: new page}.

    A shuffled copy acts like a document the model has never seen, so it can't
    jump to the right chapter from memory.
    """
    reader = PdfReader(src)
    order = list(range(len(reader.pages)))
    random.Random(seed).shuffle(order)
    writer = PdfWriter()
    for old in order:
        writer.add_page(reader.pages[old])
    writer.write(dst)
    return {old + 1: new + 1 for new, old in enumerate(order)}


def remap(items: list[dict[str, Any]], mapping: dict[int, int] | None) -> list[dict[str, Any]]:
    if not mapping:
        return items
    return [{**item, "gold": sorted(mapping[p] for p in item["gold"])} for item in items]


def server_config(data_dir: Path, roots: list[Path]) -> dict[str, Any]:
    """Launch this checkout's server with the current interpreter."""
    return {
        "command": sys.executable,
        "args": ["-m", "reader_mcp"],
        # Only reader's own settings; clients add PATH/HOME themselves, and nothing secret gets written to disk.
        "env": {"READER_DATA_DIR": str(data_dir), "READER_ALLOWED_ROOTS": os.pathsep.join(str(r) for r in roots)},
    }


@asynccontextmanager
async def reader(data_dir: Path, roots: list[Path]) -> AsyncIterator[Client]:
    cfg = server_config(data_dir, roots)
    async with Client(StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"])) as client:
        yield client


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    if result.is_error:
        raise RuntimeError(" ".join(getattr(c, "text", "") for c in result.content))
    return result.structured_content or {}

# reader-mcp

**An MCP server that turns PDFs, Word documents, CSVs and text files into searchable text and metadata for any AI agent.**

reader extracts a document once, then lets the model search and page through it instead of pasting the whole file into context.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![MCP](https://img.shields.io/badge/MCP-2026--07--28-black) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Formats:** PDF, DOCX, CSV/TSV, TXT, Markdown and JSON, detected from the file's content.
- **Metadata without an LLM:** page and word counts, title, author, dates, DOCX headings, CSV column types and scanned-PDF detection (`needs_ocr`).
- **Long documents:** ranked keyword search with page numbers and paginated reading. A 500-page PDF ingests in about 4 s.
- **Large files:** downloads and uploads stream to disk, uploads are resumable, and an ingest that outlives the client's timeout finishes in the background.
- **Secure by default:** isolated parsers with timeouts, allowlisted local paths, SSRF-protected HTTPS fetches, signed single-use upload links, optional bearer auth and automatic retention.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

**Claude Code**

```bash
claude mcp add reader -- uvx --from git+https://github.com/Anushwar/reader-mcp reader
```

**Claude Desktop, Cursor and other MCP clients**

```json
{
  "mcpServers": {
    "reader": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Anushwar/reader-mcp", "reader"]
    }
  }
}
```

Then ask: *"Use reader to ingest ~/Downloads/report.pdf and summarize the risks section."*

## Use it from your own agent

```python
import asyncio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

server = StdioServerParameters(command="uvx", args=["--from", "git+https://github.com/Anushwar/reader-mcp", "reader"])

async def main():
    async with Client(server) as reader:
        doc = (await reader.call_tool("ingest_document", {"path": "~/Documents/contract.pdf"})).structured_content
        hits = await reader.call_tool("search_documents", {"query": "termination clause", "doc_id": doc["doc_id"]})
        print(hits.structured_content)

asyncio.run(main())
```

For reliable agents, call `ingest_document` from your own code when a file arrives and give the model the `doc_id`.

**HTTP mode:** `reader --http` serves Streamable HTTP at `http://127.0.0.1:8000/mcp` and adds `create_upload_url`, for clients that don't share a disk with the server.

## How files get in

| Input | Use it when |
| --- | --- |
| `path` | The server runs on the same machine as the file. Limited to `READER_ALLOWED_ROOTS`. |
| `url` | The file has an HTTPS link, e.g. a presigned S3 URL. Google Drive, Google Docs and Dropbox share links are converted to downloads; web pages and private addresses are refused. |
| `file` | ChatGPT passes an attached file (`openai/fileParams`). |
| `upload_id` | The file is somewhere the server can't reach. `PUT` it to a link from `create_upload_url`, whole or in resumable pieces (`Content-Range`); the link also opens a browser upload page. |
| `content_base64` | The file is small. |

## Tools

| Tool | What it does |
| --- | --- |
| `ingest_document` | Extracts and stores a document; returns `doc_id`, metadata and a preview. |
| `search_documents` | Ranked keyword search across all documents or one, with page numbers. |
| `read_document` | Reads pages, sections or row blocks within a character budget, with a cursor to continue. |
| `get_metadata` | Properties, counts, CSV column profile and OCR status. |
| `list_documents` | Everything in the library. |
| `delete_document` | Removes a document and its extracted text. |
| `create_upload_url` | *(HTTP mode)* A signed, single-use, resumable upload link. |

## Configuration

All settings are optional environment variables (see [`.env.example`](.env.example)).

| Variable | Default | Purpose |
| --- | --- | --- |
| `READER_DATA_DIR` | `~/.reader-mcp` | Where documents and extracted text are stored |
| `READER_ALLOWED_ROOTS` | home folder (stdio), none (HTTP) | Folders `path` may read; hidden files are always refused |
| `READER_MAX_FILE_MB` | `200` | Largest accepted file |
| `READER_RETENTION_HOURS` | `24` | Auto-delete age (`0` keeps documents forever) |
| `READER_EXTRACT_TIMEOUT` | `120` | Seconds before an extraction is stopped |
| `READER_PUBLIC_URL` | `http://localhost:<port>` | Base URL clients use to reach the server, for upload links |
| `READER_UPLOAD_SECRET` | random per run | Signs upload links; set it so links survive restarts |
| `READER_UPLOAD_TTL` | `600` | Seconds an upload link stays valid |
| `READER_AUTH_TOKEN` | unset | Requires `Authorization: Bearer <token>` on `/mcp` |

## Evaluation

Tested on *The Hundred-Page Machine Learning Book* (152 pages). 7 factual questions were each answered in a headless Claude Code session with either reader or Claude Code's built-in PDF reading, and checked against a key written from the rendered pages.

| | reader | Built-in PDF reading |
| --- | --- | --- |
| Correct answer and page, original order | 7/7 | 7/7 |
| Total cost, original order | $0.50 | $0.90 |
| Correct answer and page, **pages shuffled** | 7/7 | 7/7 |
| Median input tokens, shuffled | **34k** | 617k |
| Median time per question, shuffled | **13 s** | 47 s |
| Total cost, shuffled | **$0.58** | $8.10 |

Shuffling the pages simulates a document the model hasn't seen before. On the original book, the built-in reader finds the right chapter from memory. Without that, it reads most of the book for each answer. Search alone (no LLM) put the correct page first for 13 of 13 questions.

```bash
uv run python eval/search.py book.pdf [--shuffle]       # search ranking, no LLM
uv run python eval/extraction.py book.pdf               # text vs. pdftotext, per-word recall
uv run python eval/agents.py book.pdf --shuffle         # headless Claude Code comparison (billed)
```

## Development

```bash
git clone https://github.com/Anushwar/reader-mcp && cd reader-mcp
uv sync
uv run pytest
```

The tests start the real server over stdio and HTTP and generate their own fixtures. To test the published build instead, set `MCP_CMD="uvx --from git+https://github.com/Anushwar/reader-mcp reader"`.

## License

MIT

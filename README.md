# reader-mcp

**An MCP server that turns PDFs, Word documents, CSVs and text files into searchable text and metadata for any AI agent.**

Point Claude Code, Claude Desktop, Cursor, ChatGPT or your own agent at a document. reader extracts it once, then lets the model search, page through and inspect it, instead of pasting the whole file into context.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![MCP](https://img.shields.io/badge/MCP-2026--07--28-black) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Formats:** PDF, DOCX, CSV/TSV, TXT, Markdown, JSON. The type is detected from the file's content, not just its extension.
- **Metadata without an LLM:** page and word counts, title, author and dates, DOCX headings and tables, CSV column types, and scanned-PDF detection (`needs_ocr`).
- **Built for context limits:** paginated reading, ranked keyword search with page numbers, and a persistent library you ingest into once.
- **Fast on long documents:** PDF text comes from pdfium, Chrome's PDF engine, so a 500-page PDF ingests in about 4 s. Files stream to disk (200 MB by default), and if a host gives up on a slow ingest, the job keeps running and a retry picks up the result.
- **Works with any host:** files arrive as a local path, an HTTPS URL, a ChatGPT file attachment, a signed upload link, or base64.
- **Safe defaults:**
  - parsers run in a separate process with a timeout
  - only allowed folders can be read
  - URL fetches are SSRF-protected and IP-pinned
  - upload links are signed and single-use
  - optional bearer auth on the HTTP endpoint
  - documents are auto-deleted after a retention period

## Quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). There's nothing else to install or clone.

**Claude Code**

```bash
claude mcp add reader -- uvx --from git+https://github.com/Anushwar/reader-mcp reader
```

**Claude Desktop, Cursor, Windsurf and most MCP clients**

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

> Desktop apps don't always see your shell's `PATH`. If the server fails to start, use the full path from `which uvx`.

Then ask: *"Use reader to ingest ~/Downloads/report.pdf and summarize the risks section."*

## Use it from your own agent

Any MCP client can launch the same command. Agent frameworks (OpenAI Agents SDK, Claude Agent SDK, LangChain, Vercel AI SDK, …) accept it as an MCP server config. With the official Python SDK:

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

**Tip for reliability:** call `ingest_document` from your own code when a file arrives, and tell the model the `doc_id`. The model then only has to decide when to search and what to read.

## ChatGPT and Claude.ai (remote)

Hosted assistants can only reach a public HTTPS server:

```bash
cloudflared tunnel --url http://localhost:8000              # or: ngrok http 8000
READER_PUBLIC_URL=https://<your-tunnel-host> uvx --from git+https://github.com/Anushwar/reader-mcp reader --http
```

Add `https://<your-tunnel-host>/mcp` as a connector: Developer mode in ChatGPT, or *Settings → Connectors* in Claude.ai. Files arrive differently on each:

- **ChatGPT** attaches the file. reader declares `openai/fileParams`, so ChatGPT passes a temporary download link and the server fetches the file itself.
- **Claude.ai** can't hand files to connectors. The model calls `create_upload_url` and uploads the file from its code sandbox with `curl`. Allow your tunnel's domain under *Settings → Capabilities → Code execution*, or open the link in a browser and upload by hand.

**Files bigger than the chat allows:** ask the assistant for an upload link (`create_upload_url`) and upload in the browser. The file goes straight to reader without passing through the chat, so only `READER_MAX_FILE_MB` applies.

A tunnel exposes the server publicly, so only keep it running while you're testing. For clients that can send headers, set `READER_AUTH_TOKEN`.

## How files get in

| Input | When to use it | Direction |
| --- | --- | --- |
| `path` | The server runs on the same machine as the files (stdio). Limited to `READER_ALLOWED_ROOTS`. | local |
| `url` | An HTTPS link to a document, e.g. a presigned S3 URL. Google Drive, Google Docs and Dropbox share links are converted automatically; web pages and private addresses are refused. | server pulls |
| `file` | ChatGPT file attachments (`openai/fileParams`). | server pulls |
| `upload_id` | The file is somewhere the server can't reach (an agent sandbox, a laptop). Get a signed link from `create_upload_url`, then `PUT` the bytes. | client pushes |
| `content_base64` | Small files only. Base64 adds ~33% and travels through the model's context. | inline |

## Tools

| Tool | What it does |
| --- | --- |
| `ingest_document` | Extracts and stores a document. Returns `doc_id`, metadata and a preview. Identical bytes are deduplicated. |
| `search_documents` | Ranked keyword search across all documents or one, returning snippets with page numbers. |
| `read_document` | Reads pages (PDF), sections (DOCX/text) or row blocks (CSV) within a character budget, with a cursor to continue. |
| `get_metadata` | Full metadata: properties, counts, CSV column profile, OCR status. |
| `list_documents` | Everything in the library. |
| `delete_document` | Removes a document and its extracted text. |
| `create_upload_url` | *(HTTP mode)* A signed, single-use upload link. |

## Configuration

Every setting is optional and comes from environment variables. See [`.env.example`](.env.example).

| Variable | Default | Purpose |
| --- | --- | --- |
| `READER_DATA_DIR` | `~/.reader-mcp` | Where documents and extracted text are stored |
| `READER_ALLOWED_ROOTS` | home folder (stdio), none (HTTP) | Folders `path` may read; hidden files are always refused |
| `READER_MAX_FILE_MB` | `200` | Largest accepted file |
| `READER_RETENTION_HOURS` | `24` | Auto-delete age (`0` keeps documents forever) |
| `READER_EXTRACT_TIMEOUT` | `120` | Seconds before an extraction is killed |
| `READER_PUBLIC_URL` | `http://localhost:<port>` | Public base URL, used for upload links and allowed hosts |
| `READER_UPLOAD_SECRET` | random per run | Signs upload links; set it so links survive restarts |
| `READER_UPLOAD_TTL` | `600` | Upload link lifetime in seconds |
| `READER_AUTH_TOKEN` | unset | Requires `Authorization: Bearer <token>` on `/mcp` |

## Evaluation

Tested on *The Hundred-Page Machine Learning Book* (152 pages). Each of 7 factual questions was answered once in a headless Claude Code session (same model), with either reader or Claude Code's built-in PDF reading. Answers and page numbers were checked against a key written from the rendered pages.

| | reader | Built-in PDF reading |
| --- | --- | --- |
| Correct answer and page, book in original order | 7/7 | 7/7 |
| Total cost, original order | $0.50 | $0.90 |
| Correct answer and page, **pages shuffled** | 7/7 | 7/7 |
| Median input tokens, shuffled | **34k** | 617k |
| Median time per question, shuffled | **13 s** | 47 s |
| Total cost, shuffled | **$0.58** | $8.10 |

Shuffling the pages simulates a document the model has never seen. On the original book, the built-in reader jumps to the right chapter from memory. Without that, it has to read most of the book to find each answer. Ingesting the book takes 1.5 s, once. Search alone (no LLM) put the correct page first for 13 of 13 questions.

Reproduce with your own copy of the book (the answer key is in [`eval/questions.json`](eval/questions.json)):

```bash
uv run python eval/search.py book.pdf [--shuffle]       # search ranking, no LLM
uv run python eval/extraction.py book.pdf               # text vs. pdftotext, per-word recall
uv run python eval/agents.py book.pdf --shuffle         # headless Claude Code comparison (billed)
```

## Limitations

- **No OCR yet.** Scanned PDFs and images are flagged with `needs_ocr`, and the extractor registry is where an OCR backend would plug in.
- **Text only.** Charts, diagrams and image content aren't extracted, and figure labels can come out in a jumbled order.
- **Keyword search, not semantic.** Paraphrased questions that share no words with the text can miss.
- **No login-protected links.** URLs must be publicly downloadable over HTTPS; for private files, use a presigned URL or an upload link.
- **No XLSX or PPTX yet.**

## Development

```bash
git clone https://github.com/Anushwar/reader-mcp && cd reader-mcp
uv sync
uv run pytest                      # launches the real server over stdio and HTTP
uv run reader --http               # or: npx @modelcontextprotocol/inspector uv run reader
```

The tests generate their own fixtures. Set `MCP_CMD="uvx --from git+https://github.com/Anushwar/reader-mcp reader"` to run them against the published build.

## License

MIT

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Wolai 知识库混合语义搜索 Skill — provides local hybrid search across the user's Wolai knowledge base. Exposes `semantic_search` and `index_status` tools via a stdio JSON-RPC MCP server.

## Commands

```bash
node scripts/index-wolai.mjs    # Full or incremental index of all Wolai pages
node scripts/mcp-server.mjs     # Start the MCP server (registered in claude.json as stdio MCP)
```

No build/lint/test steps — this is a minimal Node.js project with no dev tooling.

## Architecture

Three scripts, three responsibilities:

### `scripts/mcp-server.mjs` — MCP Server
Implements stdio JSON-RPC 2.0. Exposes two tools:
- `semantic_search(query, limit, exclude_page_ids)` → hybrid‑search result list
- `index_status` → counts and last‑index timestamp

Delegates to `db.mjs` for all data logic.

### `scripts/db.mjs` — Database & Search
Uses **sql.js** (WASM SQLite, no native dependencies). Two tables:
- `chunks` — page text chunks with vector embeddings (stored as JSON strings)
- `index_meta` — per‑page `{page_id, title, indexed_at}` for incremental tracking

Embedding: calls Ollama `nomic-embed-text` at `http://127.0.0.1:11434/api/embed`.

Search flow: semantic (cosine similarity) + keyword (term‑frequency) → **RRF fusion** (semantic k=60, keyword k=20 — lower k = higher weight). Results deduplicated, excluded page IDs filtered, sorted by combined RRF score.

### `scripts/index-wolai.mjs` — Indexer
Page discovery strategy (because Wolai's `list_docs` API caps at ~25 results):
1. `list_docs` for top‑level pages
2. Hard‑coded dashboard page ID (`kKEsWsXDv2KBEFHWqse15a`) + its parent
3. Scan each discovered page's `type=page` blocks for sub‑pages
4. Hard‑coded extra workspace pages not surfaced by the API

Text extraction: per‑block type logic (`extractBlockText`), sections grouped by heading (`extractPageText`), chunks at 500‑char max with 64‑char overlap.

Indexing: 5 concurrent page fetches, saves every 20 pages. Incremental mode skips already‑indexed pages; for small DBs (≤50 pages) checks `edited_at` to detect modifications.

## Dependencies

- **Node.js ≥ 18** with `sql.js` (only npm dependency)
- **Ollama** running locally with `nomic-embed-text` model
- **Wolai MCP** registered in Claude Code (provides `list_docs`, `get_page_blocks`, `get_doc`, `get_block` tools)
- **`.env`** file with `WOLAI_TOKEN=sk-...` for API access

## Data

`data/vectors.db` — SQLite database, git‑ignored. Generated locally per machine. Can be copied between machines to skip re‑indexing.

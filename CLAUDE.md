# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Overview

Wolai 知识库混合语义搜索 Skill — Python 实现，三路搜索（语义向量 + FTS5 全文 + trigram 模糊），RRF 融合排序。通过 stdio JSON-RPC MCP 暴露 `semantic_search` 和 `index_status` 工具。

## Commands

```bash
python scripts/index_wolai.py    # 增量索引（自动发现所有 Wolai 页面）
python scripts/mcp_server.py     # 启动 MCP server（claude.json 注册为 stdio MCP）
python scripts/test_verify.py    # 14 项可执行验证
```

## Architecture

### `scripts/db.py` — Core: DB + 3 search paths
- Python 内置 `sqlite3`，FTS5 + trigram 原生支持
- 三张表：`chunks` (embedding 存 BLOB), `fts_content` (FTS5 BM25), `fts_fuzzy` (FTS5 trigram)
- 三路搜索：semantic (Ollama + cosine), fulltext (FTS5 BM25), fuzzy (trigram + Levenshtein 并行)
- RRF 融合：K_SEM=60, K_FTS=40, K_FUZ=50 → page_id 去重 → Top N
- Embedding 内存缓存：启动时全量加载，免 JSON 解析开销

### `scripts/mcp_server.py` — MCP stdio server
- 启动时自动：FTS5 回填 → embedding 迁移 (JSON→BLOB) → 预热缓存
- 暴露 `semantic_search` 和 `index_status`

### `scripts/index_wolai.py` — Indexer
- 页面发现：`list_docs` → 自适应 `search_docs` 补漏（语言无关）→ 子页面递归扫描
- 文本提取 → heading 分段 → 500 字 chunk + 64 字重叠
- 子页面标题继承父 section 名
- 5 并发索引，每线程独立 db connection

## Dependencies

- **Python 3.8+** — 内置 sqlite3，零外部 pip 依赖
- **Ollama** — 本地运行，默认 `qwen3-embedding:0.6b`，见 `.env.example` 切换
- **Wolai MCP** — 注册在 Claude Code，提供页面读写 API
- **`.env`** — `WOLAI_TOKEN` + 可选 `WOLAI_DASHBOARD_ID`、`OLLAMA_EMBED_MODEL`

## Data

`data/vectors.db` — SQLite，`.gitignore` 排除。可跨机器复制跳过重索引。

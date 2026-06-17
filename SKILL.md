---
name: wolai-semantic-search
description: Wolai 知识库混合语义搜索。当用户用模糊描述找笔记、问"我有没有写过关于XX的内容"、或 Claude 需要从 Wolai 知识库中检索信息时自动触发。使用本地向量+全文+模糊混合搜索，无需联网。
---

# Wolai 语义搜索

为 Claude 提供 Wolai 知识库的语义搜索能力。当用户用模糊描述而不是精确标题来查找笔记时，使用此工具。

## 搜索能力

三路混合搜索 + RRF 融合排序：

- **语义搜索**：Ollama 向量余弦相似度 — "怎么让claude记住东西"→ Auto Memory
- **全文搜索**：FTS5 BM25 分词短语搜索 — 精确匹配 MCP、CLAUDE.md
- **模糊搜索**：trigram 标题匹配 + Levenshtein 兜底 — "obsidan"→"Obsidian", "clode"→"Claude"

## 维护命令

用户说以下任意一句时，运行 `python scripts/index_wolai.py` 进行增量索引：

- "更新索引"
- "更新搜索索引"
- "同步索引"

增量模式：只处理新增/修改的页面，已有索引页秒跳过。

## 技术栈

- Python 3.8+ 内置 sqlite3（FTS5 + trigram），零外部依赖
- Ollama 本地 embedding 服务
- MCP stdio 服务，claude.json 注册为 `python mcp_server.py`

## 模型选择

在 `.env` 中设置 `OLLAMA_EMBED_MODEL` 切换模型（不设置则默认 qwen3）：

| 模型 | 维度 | RAM | 适用 |
|------|------|-----|------|
| `qwen3-embedding:0.6b` | 1024 | ~2.6GB | 中英双语，语义理解强（推荐） |
| `nomic-embed-text` | 768 | ~500MB | 轻量，英文为主，快速 |
| `bge-m3` | 1024 | ~2GB | 多语言，通用性好 |

切换模型后需 `python scripts/index_wolai.py` 重建索引。

## 使用方式

Claude 自动调用，无需用户手动触发。

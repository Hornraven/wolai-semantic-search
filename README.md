# Wolai 语义搜索

为 Claude Code 提供 Wolai 知识库的本地语义搜索能力。三路混合搜索（语义向量 + FTS5 全文 + trigram 模糊），RRF 融合排序，纯本地运行。

> ⚠️ **前提条件**：需要在 Claude Code 中配置 Wolai 官方 MCP 服务，以提供基础的 Wolai API 对接能力。

## 原理

```
用户搜索 → 语义搜索 (Ollama embedding, k=60)
         → 全文搜索 (FTS5 BM25, k=40, 权重最高)
         → 模糊搜索 (trigram + Levenshtein, k=50)
         → RRF 融合 → page_id 去重 → Top 5
```

## 结构

```
scripts/
  mcp_server.py      # MCP stdio 服务（Python 内置 sqlite3）
  index_wolai.py      # 索引脚本（增量模式，5 并发，自适应发现）
  db.py               # 核心：三路搜索 + RRF 融合
  test_verify.py      # 14 项可执行验证
data/
  vectors.db          # SQLite 数据库（.gitignore 排除）
```

## 环境要求

- Python 3.8+（内置 sqlite3，零外部依赖）
- [Ollama](https://ollama.com) + embedding 模型（见下方模型选择）
- Wolai 官方 MCP（见下方配置说明）

## 模型选择

在 `.env` 中设置 `OLLAMA_EMBED_MODEL`（不设置则默认 qwen3）：

| 模型 | 维度 | RAM | 适用 |
|------|------|-----|------|
| `qwen3-embedding:0.6b` | 1024 | ~2.6GB | 中英双语，推荐 |
| `nomic-embed-text` | 768 | ~500MB | 轻量，英文为主 |
| `bge-m3` | 1024 | ~2GB | 多语言，通用 |

切换模型后需重建索引。

## 配置 Wolai MCP

1. 在 Wolai 设置 → MCP 接入中，点击「创建 Token」生成凭证
2. 在 Claude Code 中添加 MCP 服务：

```bash
claude mcp add --transport sse wolai https://api.wolai.com/v1/mcp \
  --header "Authorization: Bearer 你的Token" \
  --scope user
```

## 安装

```bash
git clone https://github.com/Hornraven/wolai-semantic-search.git
cd wolai-semantic-search
cp .env.example .env
# 编辑 .env 填入你的 WOLAI_TOKEN

# 下载 embedding 模型
ollama pull qwen3-embedding:0.6b
```

无需 `npm install`，Python 标准库即可运行。

## 首次索引

```bash
python scripts/index_wolai.py
```

自适应发现所有 Wolai 页面（list_docs + search_docs 补漏 + 递归子页面扫描），
提取文本 → Ollama embedding → 写入 SQLite + FTS5。~300 页约 30 分钟。
后续运行自动增量，秒级完成。

## 更新索引

在 Claude Code 中说"更新索引"或直接运行：

```bash
python scripts/index_wolai.py
```

增量模式：只处理新增和修改的页面，已索引页面跳过。

## MCP 配置

在 Claude Code 的 `claude.json` 中注册：

```json
{
  "mcpServers": {
    "wolai-semantic-search": {
      "command": "python",
      "args": ["<路径>/wolai-semantic-search/scripts/mcp_server.py"]
    }
  }
}
```

## 验证

```bash
python scripts/test_verify.py
```

14 项断言：FTS5 全文、trigram 模糊（obsidan→Obsidian, clode→Claude）、
语义搜索、RRF 融合去重、Ollama API。

## 跨电脑使用

1. `git clone`（无需 npm install）
2. 装 Ollama + `ollama pull qwen3-embedding:0.6b`
3. `cp .env.example .env` 并填入 Token
4. 配置 MCP
5. 运行首次索引（或从其他电脑复制 `data/vectors.db`）

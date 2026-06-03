# Wolai 语义搜索

为 Claude Code 提供 Wolai 知识库的本地语义搜索能力。用自然语言模糊查找笔记，无需精确标题。

> ⚠️ **前提条件**：本工具需要预先在 Claude Code 中安装并配置 Wolai 官方 MCP，以提供基础的 Wolai API 对接能力。

## 原理

```
用户搜索 → 语义搜索 (Ollama embedding)
        → 关键词搜索 (高频词匹配)
        → RRF 融合排序 → 返回 Top N
```

纯本地运行，不依赖任何云服务。

## 结构

```
scripts/
  index-wolai.mjs    # 索引脚本（遍历 Wolai 页面 → 提取文本 → 向量化）
  mcp-server.mjs     # MCP Server（暴露 semantic_search 工具给 Claude）
  db.mjs             # SQLite 向量库 + 混合搜索
data/
  vectors.db         # 索引数据库（.gitignore 排除，每台机器本地生成）
```

## 环境要求

- Node.js ≥ 18
- [Ollama](https://ollama.com) + nomic-embed-text 模型
- Wolai 官方 MCP（已在 Claude Code 中配置）

## 安装

```bash
git clone <repo-url>
cd wolai-semantic-search
npm install

# 下载 embedding 模型
ollama pull nomic-embed-text

# 创建 .env 写入 Wolai API Token
echo "WOLAI_TOKEN=你的token" > .env
```

## 首次索引

```bash
node scripts/index-wolai.mjs
```

遍历所有 Wolai 页面及其子页面，提取文本并生成向量索引。根据页面数量可能需要几分钟。后续运行自动增量，秒级完成。

## 更新索引

在 Claude Code 中说"更新索引"或直接运行：

```bash
node scripts/index-wolai.mjs
```

增量模式：只处理新增和修改的页面，已索引页面跳过。

## MCP 配置

在 Claude Code 的 `claude.json` 中注册：

```json
{
  "mcpServers": {
    "wolai-semantic-search": {
      "command": "node",
      "args": ["<路径>/wolai-semantic-search/scripts/mcp-server.mjs"]
    }
  }
}
```

## 跨电脑使用

1. `git clone` + `npm install`
2. 装 Ollama + `ollama pull nomic-embed-text`
3. 创建 `.env` 写入 Token
4. 配置 MCP
5. 运行首次索引（或从其他电脑复制 `data/vectors.db`）

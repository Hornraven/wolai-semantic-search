---
name: wolai-semantic-search
description: Wolai 知识库混合语义搜索。当用户用模糊描述找笔记、问"我有没有写过关于XX的内容"、或 Claude 需要从 Wolai 知识库中检索信息时自动触发。使用本地向量+全文混合搜索，无需联网。
---

# Wolai 语义搜索

为 Claude 提供 Wolai 知识库的语义搜索能力。当用户用模糊描述而不是精确标题来查找笔记时，使用此工具。

## 搜索能力

- 语义搜索：理解"怎么让claude记住东西"→ 匹配 Auto Memory 页面
- 全文搜索：精确匹配技术术语如 MCP、CLAUDE.md
- 混合排序：RRF 融合算法，综合三种搜索结果

## 维护命令

用户说以下任意一句时，运行 `scripts/index-wolai.mjs` 进行增量索引：

- "更新索引"
- "更新搜索索引"
- "同步索引"

增量模式：只处理新增/修改的页面，290 已索引页秒跳过。

## 使用方式

Claude 自动调用，无需用户手动触发。

## MCP 配置

已在 claude.json 中注册为 stdio MCP。

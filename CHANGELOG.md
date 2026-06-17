# Changelog — Python 重写 + 持续优化

> 原 repo: [Hornraven/wolai-semantic-search](https://github.com/Hornraven/wolai-semantic-search)

---

## 2026-06-17 — 搜索质量 + 性能 + 通用化

**搜索质量**
- ✅ FTS5 中英混合查询拆分 (`python函数` → `python OR 函数`)
- ✅ Levenshtein 从兜底改为并行搜索路径 (`clode` → `Claude`)
- ✅ FTS5 短 ASCII 前缀匹配 (`nas*` 避免匹配 `monastic`)
- ✅ fulltext / fuzzy 双路 page_id 去重

**性能**
- ✅ Embedding JSON TEXT → BLOB (`struct.pack`)，省 60% 存储
- ✅ Embedding 内存缓存，搜索 40-68s → ~7s
- ✅ 索引并发化 (ThreadPoolExecutor, 5 workers)

**通用化**
- ✅ 移除硬编码个人页面 ID (DASHBOARD_ID, EXTRA_PAGES, exclude_page_ids)
- ✅ 自适应 search_docs 补漏 — 从已知标题提取种子字符，语言无关
- ✅ 模型可选 (OLLAMA_EMBED_MODEL): qwen3 / nomic / bge-m3
- ✅ 维度自适应测试 (768 / 1024)
- ✅ .env.example 完善文档

**修复**
- ✅ list_docs 25 页截断 → search_docs 补漏 (303 → 1617 页)
- ✅ 删除旧 JS 文件 (.mjs, package.json)，纯 Python 项目

---

## 2026-06-16 — Python 重写

**为什么重写：** 旧版 `sql.js` (WASM SQLite) 不支持 FTS5，全文搜索退化为 `String.indexOf()`，模糊搜索不存在。

**改动：**
- `scripts/db.py` — SQLite + FTS5 + trigram + 向量，零外部依赖
- `scripts/mcp_server.py` — MCP stdio JSON-RPC
- `scripts/index_wolai.py` — 增量索引，5 并发
- `scripts/test_verify.py` — 14 项可执行验证

**搜索架构：** 三路并行 (语义 + FTS5 BM25 + trigram 模糊) → RRF 融合 → Top N

**关键修复：**
- nomic-embed-text → qwen3-embedding:0.6b（中英双语，1024 维）
- Wolai API 代理绕过
- 子页面标题上下文继承 (父 section 名注入)

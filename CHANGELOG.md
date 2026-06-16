# Changelog — Python 重写 + FTS5 trigram 模糊搜索

> 日期：2026-06-16
> 原 repo: [Hornraven/wolai-semantic-search](https://github.com/Hornraven/wolai-semantic-search)

---

## 改动总览

| 文件 | 状态 | 说明 |
|------|------|------|
| `scripts/db.py` | **新增** | SQLite + FTS5 + trigram + 向量搜索核心（Python 内置 sqlite3） |
| `scripts/mcp_server.py` | **新增** | MCP stdio JSON-RPC 服务 |
| `scripts/index_wolai.py` | **新增** | Wolai 页面索引器（增量模式） |
| `scripts/test_verify.py` | **新增** | 可执行验证脚本（14 项测试） |
| `scripts/db.mjs` | 保留 | 旧 JS 版（不再使用） |
| `scripts/mcp-server.mjs` | 保留 | 旧 JS 版 |
| `scripts/index-wolai.mjs` | 保留 | 旧 JS 版 |

---

## 为什么重写

旧版用 `sql.js`（WASM SQLite），**不支持 FTS5 扩展**。设计文档画了三路搜索（语义+全文+模糊），但实际代码里全文搜索退化成 `String.indexOf()` 暴力遍历，模糊搜索根本不存在。

Python 内置 `sqlite3` 原生支持 FTS5 + trigram，不需要任何外部依赖。

---

## 搜索架构（三路 + RRF 融合）

```
用户输入 → 三分路并行:
  1. 语义搜索（Ollama embedding + 余弦相似度）  → nomic-embed-text / qwen3-embedding
  2. 全文搜索（FTS5 BM25 排名）                → SQLite FTS5
  3. 模糊搜索（trigram containment + 编辑距离兜底）
     ↓
  RRF 融合排序 → page_id 去重 → Top N
```

### 模糊搜索详细设计

- **trigram containment**：只匹配标题，容忍拼写错误
  - `obsidan` → `Obsidian`（3/5 trigrams 命中）
  - 阈值 ≥ 0.45（约一半 trigram 命中）
- **短词兜底**（< 3 字）：直接 substring 匹配
  - `甜点` → 标题 `🧁甜点 > 提拉米苏`
- **编辑距离兜底**：trigram 零命中时（如 `clode` → `Claude`），阈值 ≥ 0.5
- **子页面 context**：父页面 heading 自动注入子页面标题
  - `美食 > 🧁甜点 > 提拉米苏`（而非 `美食 > 提拉米苏`）

---

## 关键修复

| 问题 | 修复 |
|------|------|
| sql.js 不支持 FTS5 | 换 Python 内置 sqlite3 |
| Wolai API 被代理卡住 | `urllib` 绕过代理直连 |
| .env token 过期 | 更新为 sk- 前缀 token |
| 子页面 section 名丢失 | `index_page` 跟踪父 heading 注入子页面标题 |
| 短词（2 字）搜不到 | `_trigram_similarity` 加 substring 兜底 |
| 同一页面多标题被去重 | `fuzzy_search` 改 `DISTINCT page_id, title` |

---

## 验证

```bash
python scripts/test_verify.py
# ALL 14/14 PASSED
```

涵盖：FTS5 创建、trigram 相似度、全文搜索、模糊搜索、触发器同步、RRF 融合、Ollama embedding。

---

## 待优化

- `nomic-embed-text` 语义理解弱 → 考虑换 `qwen3-embedding`
- `clode` → `Claude` 零 trigram 重叠 → 编辑距离兜底触发，但阈值高
- 索引器序号反复跑（同页面多次被 `discover_all_pages` 扫到）→ 需要稳定去重

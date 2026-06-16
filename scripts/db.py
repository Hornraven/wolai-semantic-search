"""Wolai 语义搜索 — 数据库 + 混合搜索层。

Python 内置 sqlite3 原生支持 FTS5 trigram，无需任何外部依赖。
"""
import json
import math
import sqlite3
import urllib.request
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "vectors.db"
OLLAMA_URL = "http://127.0.0.1:11434/api/embed"

# ── Database ──────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    init_schema(db)
    return db


def init_schema(db: sqlite3.Connection) -> None:
    """创建所有表和索引。幂等——已存在的不会重复创建。"""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL,
            title TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding TEXT,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_page_id ON chunks(page_id);

        -- FTS5 全文搜索（默认分词器，BM25 排名）
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(
            title, chunk_text, content='chunks', content_rowid='id'
        );

        -- FTS5 模糊搜索（trigram 分词器，容忍拼写错误）
        -- 搜 "obsidan" 能匹配 "obsidian"，搜 "claude" 能匹配 "clode"
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_fuzzy USING fts5(
            title, chunk_text, tokenize='trigram', content='chunks', content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS index_meta (
            page_id TEXT PRIMARY KEY,
            title TEXT,
            block_count INTEGER,
            indexed_at INTEGER NOT NULL
        );

        -- FTS5 同步触发器：插入/删除 chunks 时自动维护索引
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO fts_content(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
            INSERT INTO fts_fuzzy(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO fts_content(fts_content, rowid, title, chunk_text) VALUES('delete', old.id, old.title, old.chunk_text);
            INSERT INTO fts_fuzzy(fts_fuzzy, rowid, title, chunk_text) VALUES('delete', old.id, old.title, old.chunk_text);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO fts_content(fts_content, rowid, title, chunk_text) VALUES('delete', old.id, old.title, old.chunk_text);
            INSERT INTO fts_content(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
            INSERT INTO fts_fuzzy(fts_fuzzy, rowid, title, chunk_text) VALUES('delete', old.id, old.title, old.chunk_text);
            INSERT INTO fts_fuzzy(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
        END;
    """)
    db.commit()


def backfill_fts(db: sqlite3.Connection) -> int:
    """把已有 chunks 数据回填到 FTS5 索引表。返回填充条数。"""
    rows = db.execute("SELECT id, title, chunk_text FROM chunks").fetchall()
    count = 0
    for row in rows:
        db.execute(
            "INSERT OR IGNORE INTO fts_content(rowid, title, chunk_text) VALUES (?, ?, ?)",
            (row["id"], row["title"], row["chunk_text"])
        )
        db.execute(
            "INSERT OR IGNORE INTO fts_fuzzy(rowid, title, chunk_text) VALUES (?, ?, ?)",
            (row["id"], row["title"], row["chunk_text"])
        )
        count += 1
    db.commit()
    return count


# ── Embedding ─────────────────────────────────────────────

def get_embedding(text: str) -> list[float]:
    """调用 Ollama 获取文本向量。"""
    body = json.dumps({"model": "nomic-embed-text", "input": text}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data["embeddings"][0]


def get_embeddings(batch: list[str]) -> list[list[float]]:
    """批量获取向量。"""
    body = json.dumps({"model": "nomic-embed-text", "input": batch}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data["embeddings"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Indexing ──────────────────────────────────────────────

def insert_chunk(db: sqlite3.Connection, page_id: str, title: str,
                 chunk_index: int, chunk_text: str, embedding: list[float]) -> int:
    text = chunk_text.replace("\0", "")  # SQLite 不接受 null 字节
    now = int(__import__("time").time())
    cur = db.execute(
        "INSERT INTO chunks (page_id, title, chunk_index, chunk_text, embedding, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (page_id, title, chunk_index, text, json.dumps(embedding), now)
    )
    db.commit()
    return cur.lastrowid


def set_index_meta(db: sqlite3.Connection, page_id: str, title: str, block_count: int) -> None:
    now = int(__import__("time").time())
    db.execute(
        "INSERT OR REPLACE INTO index_meta (page_id, title, block_count, indexed_at) "
        "VALUES (?, ?, ?, ?)",
        (page_id, title, block_count, now)
    )
    db.commit()


def get_indexed_pages(db: sqlite3.Connection) -> dict[str, dict]:
    rows = db.execute("SELECT page_id, title, indexed_at FROM index_meta").fetchall()
    return {r["page_id"]: dict(r) for r in rows}


# ── Three search paths ────────────────────────────────────

def semantic_search(db: sqlite3.Connection, query_vec: list[float],
                    limit: int = 10) -> list[dict]:
    """向量语义搜索：余弦相似度排序。"""
    rows = db.execute(
        "SELECT id, page_id, title, chunk_index, chunk_text, embedding "
        "FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return []

    scored = []
    for row in rows:
        emb = json.loads(row["embedding"])
        score = cosine_similarity(query_vec, emb)
        scored.append({**dict(row), "score": score})

    scored.sort(key=lambda r: r["score"], reverse=True)

    # 兜底：如果 trigram 没找到，用编辑距离再试（处理 'clode'→'Claude' 这种零重叠极端情况）
    if not scored and len(sanitized) >= 3:
        rows2 = db.execute(
            "SELECT DISTINCT page_id, title FROM chunks ORDER BY LENGTH(title) DESC"
        ).fetchall()
        for row in rows2:
            pid = row["page_id"]
            title = row["title"] or ""
            lev_score = _levenshtein_ratio(sanitized, title)
            if lev_score >= 0.5:
                scored.append({
                    "id": 0, "page_id": pid, "title": title,
                    "chunk_index": 0, "chunk_text": title,
                    "score": lev_score,
                })

    for i, r in enumerate(scored[:limit]):
        r["rank"] = i + 1
    return scored[:limit]


def fulltext_search(db: sqlite3.Connection, query: str,
                    limit: int = 10) -> list[dict]:
    """FTS5 全文搜索：BM25 排名。"""
    # FTS5 MATCH 语法：用 * 做前缀匹配，双引号做短语
    sanitized = query.replace('"', '').strip()
    if not sanitized:
        return []

    try:
        rows = db.execute(
            "SELECT c.id, c.page_id, c.title, c.chunk_index, c.chunk_text, "
            "fts_content.rank AS bm25_score "
            "FROM fts_content "
            "JOIN chunks c ON c.id = fts_content.rowid "
            "WHERE fts_content MATCH ? "
            "ORDER BY bm25_score LIMIT ?",
            (sanitized, limit)
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    results = []
    for i, row in enumerate(rows):
        results.append({
            "id": row["id"], "page_id": row["page_id"], "title": row["title"],
            "chunk_index": row["chunk_index"], "chunk_text": row["chunk_text"],
            "score": -row["bm25_score"],  # BM25: lower = better, flip sign
            "rank": i + 1,
        })
    return results


def _extract_trigrams(text: str) -> set[str]:
    """提取文本的所有三字符组合（trigram）。用于模糊匹配。"""
    s = text.lower()
    return {s[i:i+3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def _trigram_similarity(query: str, text: str) -> float:
    """Containment 得分：query 的 trigram 有多少比例在 text 中出现。

    - 短词（< 3 字）：直接检查 query 是否是 text 的子串
    - 容忍拼写错误：'obsidan' vs 'Obsidian...' → 3/5 = 0.6
    - 完全匹配：'obsidian' vs 'Obsidian...' → 5/6 ≈ 0.83
    - 完全不相关 → 0.0
    """
    if not query or not text:
        return 0.0
    q_lower = query.lower()
    t_lower = text.lower()
    # 短词走 substring 匹配
    if len(query) < 3:
        return 1.0 if q_lower in t_lower else 0.0
    q_tri = _extract_trigrams(query)
    t_tri = _extract_trigrams(text)
    if not q_tri:
        return 0.0
    matched = sum(1 for t in q_tri if t in t_tri)
    return matched / len(q_tri)


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """编辑距离相似度：1.0 = 完全相同，0.0 = 完全不同。

    'clode' vs 'claude' → 约 0.67，'mcp' vs 'typescript' → 0.0
    """
    if not s1 or not s2:
        return 0.0
    s1, s2 = s1.lower(), s2.lower()
    if s1 == s2:
        return 1.0

    # 用两行滚动数组计算 Levenshtein 距离
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1, 1):
        curr = [i]
        for j, c2 in enumerate(s2, 1):
            curr.append(min(
                prev[j] + 1,           # 删除
                curr[j - 1] + 1,       # 插入
                prev[j - 1] + (0 if c1 == c2 else 1)  # 替换
            ))
        prev = curr

    max_len = max(len(s1), len(s2))
    return 1.0 - (prev[-1] / max_len)


def fuzzy_search(db: sqlite3.Connection, query: str,
                 limit: int = 10, min_score: float = 0.45) -> list[dict]:
    """模糊搜索：用 trigram containment 匹配标题，容忍拼写错误。

    - 只在标题上匹配（照搬 Obsidian 设计），避免正文噪音
    - 'obsidan' → 标题含 'Obsidian' 的页面（3/5 trigrams 命中 → 0.6）
    - 'clode' → 标题含 'Claude' 的页面（'ode' 命中 'Code' → 0.33）
    - 'clode' → 标题 'blender'（0 trigrams 命中 → 0.0）✅ 不误匹配
    - 'MCP' → 标题含 'MCP' 的页面（1.0），不返回正文碰巧有 MCP 的无关页面
    """
    sanitized = query.strip().lower()
    if not sanitized or len(sanitized) < 2:
        return []

    # 去重 title（同一页面的不同子页面有不同 title，不能只按 page_id 去重）
    rows = db.execute(
        "SELECT DISTINCT page_id, title FROM chunks ORDER BY LENGTH(title) DESC"
    ).fetchall()

    scored = []
    for row in rows:
        title = row["title"] or ""
        page_id = row["page_id"]
        score = _trigram_similarity(sanitized, title)
        if score >= min_score:
            scored.append({
                "id": 0, "page_id": page_id, "title": title,
                "chunk_index": 0, "chunk_text": title, "score": score,
            })

    scored.sort(key=lambda r: r["score"], reverse=True)

    # 兜底：如果 trigram 没找到，用编辑距离再试（处理 'clode'→'Claude' 这种零重叠极端情况）
    if not scored and len(sanitized) >= 3:
        rows2 = db.execute(
            "SELECT DISTINCT page_id, title FROM chunks ORDER BY LENGTH(title) DESC"
        ).fetchall()
        for row in rows2:
            pid = row["page_id"]
            title = row["title"] or ""
            lev_score = _levenshtein_ratio(sanitized, title)
            if lev_score >= 0.5:
                scored.append({
                    "id": 0, "page_id": pid, "title": title,
                    "chunk_index": 0, "chunk_text": title,
                    "score": lev_score,
                })

    for i, r in enumerate(scored[:limit]):
        r["rank"] = i + 1
    return scored[:limit]


# ── RRF 融合 ──────────────────────────────────────────────

def hybrid_search(db: sqlite3.Connection, query: str, limit: int = 5,
                  exclude_page_ids: Optional[list[str]] = None) -> list[dict]:
    """三路并行搜索 + RRF 融合排序。"""
    if exclude_page_ids is None:
        exclude_page_ids = []

    # RRF 常量：值越小该路权重越高
    K_SEM = 60   # 语义搜索
    K_FTS = 40   # 全文搜索
    K_FUZ = 50   # 模糊搜索

    combined: dict[int, dict] = {}

    # 1. 语义搜索
    try:
        q_vec = get_embedding(query)
        for r in semantic_search(db, q_vec, 10):
            combined[r["id"]] = {**r, "rrf_score": 1.0 / (K_SEM + r["rank"])}
    except Exception as e:
        print(f"[warn] Semantic search failed: {e}", flush=True)

    # 2. 全文搜索 (FTS5 BM25)
    for r in fulltext_search(db, query, 10):
        bonus = 1.0 / (K_FTS + r["rank"])
        if r["id"] in combined:
            combined[r["id"]]["rrf_score"] += bonus
        else:
            combined[r["id"]] = {**r, "rrf_score": bonus}

    # 3. 模糊搜索 (FTS5 trigram)
    for r in fuzzy_search(db, query, 10):
        bonus = 1.0 / (K_FUZ + r["rank"])
        if r["id"] in combined:
            combined[r["id"]]["rrf_score"] += bonus
        else:
            combined[r["id"]] = {**r, "rrf_score": bonus}

    # 排序，按 page_id 去重（取最高分的那条），再去排除
    results = sorted(combined.values(), key=lambda r: r["rrf_score"], reverse=True)
    exclude_set = set(exclude_page_ids)

    seen_pages = set()
    output = []
    for r in results:
        if r["page_id"] in exclude_set:
            continue
        if r["page_id"] in seen_pages:
            continue
        seen_pages.add(r["page_id"])
        output.append({
            "page_id": r["page_id"],
            "title": r["title"],
            "snippet": _truncate(r.get("chunk_text", ""), 150),
            "score": round(r["rrf_score"] * 1000) / 1000,
        })
        if len(output) >= limit:
            break
    return output


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."

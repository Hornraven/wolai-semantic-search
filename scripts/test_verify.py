#!/usr/bin/env python3
"""Wolai 语义搜索 — 可执行验证脚本。

每个测试独立运行，验证设计文档中的一项承诺。
最后一行必然是 "ALL X/Y PASSED" 或 "X/Y FAILED"。
"""
import json
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PASS = 0
FAIL = 0
SKIP = 0

def run_test(name, fn):
    global PASS, FAIL, SKIP
    try:
        result = fn()
        if result is True:
            PASS += 1
            print(f"  ✅ {name}")
        elif result is False:
            FAIL += 1
            print(f"  ❌ {name}")
        else:
            SKIP += 1
            print(f"  ⏭️  {name} — SKIPPED ({result})")
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {name} — {e}")


# ═══════════════════════════════════════════════════════════
#  DB 工具
# ═══════════════════════════════════════════════════════════

def temp_db():
    """创建临时数据库（每测试独立，用唯一文件名）。"""
    import random
    path = Path(tempfile.gettempdir()) / f"_wv_{random.randint(0, 999999)}.db"
    return sqlite3.connect(str(path))


def seed_db(db):
    """插入测试数据 + 建 FTS5 索引。"""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL, title TEXT NOT NULL,
            chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL,
            embedding TEXT, updated_at INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(
            title, chunk_text, content='chunks', content_rowid='id'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_fuzzy USING fts5(
            title, chunk_text, tokenize='trigram', content='chunks', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO fts_content(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
            INSERT INTO fts_fuzzy(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
        END;
    """)

    items = [
        ("page_1", "Auto Memory", 0,
         "Auto Memory 功能让 Claude 自动记住用户偏好和上下文信息",
         json.dumps([0.1] * 1024)),
        ("page_1", "Auto Memory", 1,
         "配置方法：在 CLAUDE.md 中添加 memory 指令",
         json.dumps([0.2] * 1024)),
        ("page_2", "Claude Code 插件系统", 0,
         "Claude Code 支持通过 MCP 协议扩展功能，包括文件操作、搜索等",
         json.dumps([0.3] * 1024)),
        ("page_2", "Claude Code 插件系统", 1,
         "安装插件：claude plugins install <name>，支持 GitHub 和 npm",
         json.dumps([0.4] * 1024)),
        ("page_3", "Obsidian 知识管理", 0,
         "Obsidian 使用本地 Markdown 文件管理知识，支持双向链接和图谱视图",
         json.dumps([0.5] * 1024)),
        ("page_4", "UE5 光照烘焙", 0,
         "静态光照烘焙使用 Lightmass，需要设置 Lightmap Resolution",
         json.dumps([0.6] * 1024)),
    ]
    import time
    now = int(time.time())
    for pid, title, ci, text, emb in items:
        db.execute(
            "INSERT INTO chunks (page_id, title, chunk_index, chunk_text, embedding, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, title, ci, text, emb, now)
        )
    db.commit()


def ollama_up():
    try:
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:11434/api/tags"), timeout=2
        )
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
#  第 1 组：数据库基础
# ═══════════════════════════════════════════════════════════

def t_1_1():
    """Python sqlite3 支持 FTS5"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
    db.execute("INSERT INTO t VALUES ('hello world')")
    rows = db.execute("SELECT * FROM t WHERE t MATCH 'hello'").fetchall()
    db.close()
    assert len(rows) == 1, f"FTS5 not working, got {len(rows)}"
    return True


def t_1_2():
    """Python trigram 相似度（替代 FTS5 trigram）"""
    from db import _extract_trigrams, _trigram_similarity
    # obsidian 的 trigrams
    tri = _extract_trigrams("obsidian")
    assert "obs" in tri, f"expected 'obs' in trigrams, got {tri}"
    assert len(tri) == 6, f"expected 6 trigrams for 'obsidian', got {len(tri)}"
    # obsidan (少 i) 的相似度应该 > 0.4
    sim = _trigram_similarity("obsidan", "obsidian 知识管理")
    assert sim > 0.4, f"trigram similarity too low: {sim}"
    # 完全匹配应该高
    sim2 = _trigram_similarity("obsidian", "obsidian 知识管理")
    assert sim2 > sim, f"exact match {sim2} should be higher than fuzzy {sim}"
    return True


def t_1_3():
    """schema 初始化建表完整"""
    db = temp_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL, title TEXT NOT NULL,
            chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL,
            embedding TEXT, updated_at INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(title, chunk_text, content='chunks', content_rowid='id');
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_fuzzy USING fts5(title, chunk_text, tokenize='trigram', content='chunks', content_rowid='id');
        CREATE TABLE IF NOT EXISTS index_meta (page_id TEXT PRIMARY KEY, title TEXT, block_count INTEGER, indexed_at INTEGER NOT NULL);
    """)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
    db.close()
    required = {"chunks", "fts_content", "fts_fuzzy", "index_meta"}
    assert required <= tables, f"Missing: {required - tables}"
    return True


# ═══════════════════════════════════════════════════════════
#  第 2 组：搜索功能
# ═══════════════════════════════════════════════════════════

def t_2_1():
    """FTS5 全文搜索"""
    db = temp_db()
    seed_db(db)
    rows = db.execute(
        "SELECT c.page_id FROM fts_content JOIN chunks c ON c.id = fts_content.rowid "
        "WHERE fts_content MATCH 'Auto Memory' ORDER BY rank LIMIT 5"
    ).fetchall()
    db.close()
    pids = {r[0] for r in rows}
    assert "page_1" in pids, f"fulltext 'Auto Memory' should find page_1, got {pids}"
    return True


def t_2_2():
    """trigram 模糊搜索 'obsidan' → 'Obsidian'"""
    import db as db_mod
    db_mod.DB_PATH = Path(tempfile.gettempdir()) / "_wv_fz2.db"
    if db_mod.DB_PATH.exists():
        db_mod.DB_PATH.unlink()
    db = db_mod.get_db()
    seed_db(db)
    from db import backfill_fts, fuzzy_search
    backfill_fts(db)
    results = fuzzy_search(db, "obsidan", limit=5)
    db.close()
    db_mod.DB_PATH.unlink(missing_ok=True)
    pids = {r["page_id"] for r in results}
    assert "page_3" in pids, f"trigram 'obsidan' should find page_3 (Obsidian), got {pids}"
    return True


def t_2_3():
    """trigram 模糊搜索 'clode' → 'Claude'"""
    import db as db_mod
    db_mod.DB_PATH = Path(tempfile.gettempdir()) / "_wv_fz3.db"
    if db_mod.DB_PATH.exists():
        db_mod.DB_PATH.unlink()
    db = db_mod.get_db()
    seed_db(db)
    from db import backfill_fts, fuzzy_search
    backfill_fts(db)
    results = fuzzy_search(db, "clode", limit=5)
    db.close()
    db_mod.DB_PATH.unlink(missing_ok=True)
    pids = {r["page_id"] for r in results}
    assert "page_2" in pids, f"trigram 'clode' should find page_2 (Claude), got {pids}"
    return True


def t_2_4():
    """精确词搜索不受影响"""
    db = temp_db()
    seed_db(db)
    rows = db.execute(
        "SELECT c.page_id FROM fts_content JOIN chunks c ON c.id = fts_content.rowid "
        "WHERE fts_content MATCH '光照烘焙' ORDER BY rank LIMIT 5"
    ).fetchall()
    db.close()
    pids = {r[0] for r in rows}
    assert "page_4" in pids, f"exact match '光照烘焙' should find page_4, got {pids}"
    return True


def t_2_5():
    """FTS5 触发器自动同步"""
    db = temp_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, page_id TEXT, title TEXT,
            chunk_index INTEGER, chunk_text TEXT, embedding TEXT, updated_at INTEGER
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(title, chunk_text, content='chunks', content_rowid='id');
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_fuzzy USING fts5(title, chunk_text, tokenize='trigram', content='chunks', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO fts_content(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
            INSERT INTO fts_fuzzy(rowid, title, chunk_text) VALUES (new.id, new.title, new.chunk_text);
        END;
    """)
    import time
    db.execute(
        "INSERT INTO chunks (page_id, title, chunk_index, chunk_text, embedding, updated_at) "
        "VALUES ('tp', '测试', 0, 'FTS5触发器同步测试数据', '[]', ?)", (int(time.time()),)
    )
    db.commit()
    c1 = db.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0]
    c2 = db.execute("SELECT COUNT(*) FROM fts_fuzzy").fetchone()[0]
    db.close()
    assert c1 >= 1, f"Triggers failed: fts_content should have entries, got {c1}"
    return True


def t_2_6():
    """RRF 融合通过 db.hybrid_search（需要 Ollama）"""
    if not ollama_up():
        return "Ollama not running"
    import db as db_mod
    db_mod.DB_PATH = Path(tempfile.gettempdir()) / "_wv_hybrid.db"
    if db_mod.DB_PATH.exists():
        db_mod.DB_PATH.unlink()
    db = db_mod.get_db()
    seed_db(db)
    from db import backfill_fts
    backfill_fts(db)
    from db import hybrid_search
    results = hybrid_search(db, "Claude 记忆", limit=5)
    db.close()
    db_mod.DB_PATH.unlink(missing_ok=True)
    assert len(results) > 0, "hybrid search returned 0 results"
    pids = [r["page_id"] for r in results]
    assert len(pids) == len(set(pids)), f"duplicate page_ids: {pids}"
    return True


# ═══════════════════════════════════════════════════════════
#  第 3 组：依赖
# ═══════════════════════════════════════════════════════════

def t_3_1():
    """纯 Python 内置 sqlite3，不依赖 sql.js"""
    import sqlite3 as s
    assert s.sqlite_version
    return True


def t_3_2():
    """不依赖 better-sqlite3"""
    try:
        import better_sqlite3  # noqa
        return "better-sqlite3 installed (optional, not required)"
    except ImportError:
        return True


def t_3_3():
    """Ollama 连接"""
    if ollama_up():
        return True
    return "Ollama not running"


# ═══════════════════════════════════════════════════════════
#  第 4 组：语义搜索（需要 Ollama）
# ═══════════════════════════════════════════════════════════

def t_4_1():
    """Ollama embedding API"""
    if not ollama_up():
        return "Ollama not running"
    import db as db_mod
    vec = db_mod.get_embedding("测试文本")
    dims = len(vec)
    assert dims in (768, 1024), f"expected 768 or 1024 dims, got {dims}"
    return True


def t_4_2():
    """语义搜索：'让 AI 记住偏好' → Auto Memory"""
    if not ollama_up():
        return "Ollama not running"
    import db as db_mod
    db_mod.DB_PATH = Path(tempfile.gettempdir()) / "_wv_sem.db"
    if db_mod.DB_PATH.exists():
        db_mod.DB_PATH.unlink()
    db = db_mod.get_db()
    seed_db(db)
    from db import backfill_fts, hybrid_search
    backfill_fts(db)
    results = hybrid_search(db, "让 AI 记住偏好", limit=5, exclude_page_ids=[])
    db.close()
    db_mod.DB_PATH.unlink(missing_ok=True)
    titles = [r["title"] for r in results]
    assert any("Memory" in t for t in titles), f"semantic search missed Auto Memory: {titles}"
    return True


# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════

TESTS = [
    ("第 1 组：数据库基础", [
        ("Python sqlite3 支持 FTS5", t_1_1),
        ("Python trigram 模糊相似度", t_1_2),
        ("schema 初始化完整", t_1_3),
    ]),
    ("第 2 组：搜索功能", [
        ("FTS5 全文搜索 'Auto Memory' → page_1", t_2_1),
        ("trigram 模糊搜索 'obsidan' → Obsidian", t_2_2),
        ("trigram 模糊搜索 'clode' → Claude", t_2_3),
        ("精确词 '光照烘焙' 仍然搜到", t_2_4),
        ("FTS5 触发器自动同步", t_2_5),
        ("RRF 三路融合排序", t_2_6),
    ]),
    ("第 3 组：依赖检查", [
        ("不依赖 sql.js / node", t_3_1),
        ("不依赖 better-sqlite3", t_3_2),
        ("Ollama 连接", t_3_3),
    ]),
    ("第 4 组：语义搜索（需 Ollama）", [
        ("Ollama embedding API", t_4_1),
        ("语义搜索 '让 AI 记住偏好' → Auto Memory", t_4_2),
    ]),
]

if __name__ == "__main__":
    print("=" * 60)
    print("Wolai Semantic Search — 可执行验证")
    print(f"Python: {sys.version.split()[0]}")
    print("=" * 60)

    for section_name, items in TESTS:
        print(f"\n{section_name}")
        for name, fn in items:
            run_test(name, fn)

    total = PASS + FAIL + SKIP
    print("\n" + "=" * 60)
    if FAIL == 0:
        print(f"ALL {PASS}/{total} PASSED  ({SKIP} skipped)")
    else:
        print(f"{FAIL}/{total} FAILED  ({PASS} passed, {SKIP} skipped)")
    print("=" * 60)

    sys.exit(0 if FAIL == 0 else 1)

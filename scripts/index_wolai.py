#!/usr/bin/env python3
"""Wolai 语义搜索 — 索引脚本。

遍历 Wolai 所有页面 → 提取文本 → 分块 → Ollama embedding → 写入 SQLite + FTS5。
增量模式：只处理新增/修改页面。
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import (
    get_db, get_embeddings, insert_chunk, set_index_meta, get_indexed_pages,
    backfill_fts
)

# ── Config ────────────────────────────────────────────────

def load_token() -> str:
    """从 .env 或环境变量读取 Wolai API Token。"""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("WOLAI_TOKEN="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("WOLAI_TOKEN", "")


TOKEN = load_token()
API_URL = "https://api.wolai.com/v1/mcp/"
CONCURRENCY = 5
DASHBOARD_ID = "kKEsWsXDv2KBEFHWqse15a"

req_seq = 0

def next_id() -> int:
    global req_seq
    req_seq += 1
    return req_seq


# ── Wolai API ─────────────────────────────────────────────

def wolai_call(method: str, params: dict) -> list[dict]:
    """调用 Wolai MCP API，返回 SSE 事件列表。"""
    body = json.dumps({
        "jsonrpc": "2.0", "id": next_id(), "method": method, "params": params
    }).encode()
    # 绕过代理直连 Wolai（代理可能导致 HTTPS 挂起）
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    })
    with opener.open(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def call_tool(name: str, args: dict) -> dict:
    """调用 Wolai MCP tool，返回解析后的数据。"""
    results = wolai_call("tools/call", {"name": name, "arguments": args})
    r = results[0].get("result", {}) if results else {}
    content = (r.get("content") or [{}])[0]
    if r.get("isError"):
        raise RuntimeError(f"Tool {name} error: {content.get('text', '')}")
    parsed = json.loads(content.get("text", "{}"))
    if "data" in parsed and "data" in parsed["data"]:
        return parsed["data"]["data"]       # list_docs, get_page_blocks
    if "data" in parsed and "children" in parsed["data"]:
        return parsed["data"]                # get_block → {block, children}
    return parsed


# ── Text extraction & chunking ────────────────────────────

def extract_block_text(block: dict) -> str:
    """从 Wolai block 中提取纯文本。"""
    # 表格
    if block.get("type") == "simple_table" and block.get("table_content"):
        return "\n".join(
            " | ".join(
                "".join(c.get("title", "") for c in (cell.get("content") or []))
                for cell in row
            )
            for row in block["table_content"]
        ).strip()

    # 文件
    if block.get("type") == "file" and block.get("caption"):
        return block["caption"].strip()

    content = block.get("content")
    if not content or not isinstance(content, list):
        if block.get("caption"):
            return block["caption"].strip()
        return ""

    is_code = block.get("type") in ("code", "equation", "block_equation")
    sep = "\n" if is_code else ""
    parts = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
        elif c.get("type") == "bi_link":
            parts.append(c.get("title", ""))
        else:
            parts.append(c.get("title", ""))
    return sep.join(parts).strip()


def extract_page_text(blocks: list[dict]) -> list[dict]:
    """按 heading 分段提取页面文本。返回 [{heading, text}]。"""
    sections = []
    current_heading = ""
    current_texts = []

    text_types = {
        "text", "bull_list", "enum_list", "todo_list", "code",
        "quote", "callout", "toggle_list", "simple_table", "database",
        "template_button", "file",
    }

    for block in blocks:
        text = extract_block_text(block)
        if not text:
            continue
        if block.get("type") == "heading":
            if current_texts:
                combined = "\n".join(current_texts).strip()
                if len(combined) > 5:
                    sections.append({"heading": current_heading, "text": combined})
            current_heading = text
            current_texts = []
        elif block.get("type") in text_types:
            current_texts.append(text)

    if current_texts:
        combined = "\n".join(current_texts).strip()
        if len(combined) > 5:
            sections.append({"heading": current_heading, "text": combined})
    elif current_heading:
        # heading 下面没有正文（只有子页面的情况）
        # 仍然保留 heading 本身作为可搜索文本
        sections.append({"heading": current_heading, "text": current_heading})

    return sections


def split_chunks(sections: list[dict], max_len: int = 500,
                 overlap: int = 64) -> list[str]:
    """把 sections 切成固定大小的 chunk。"""
    chunks = []
    for s in sections:
        prefix = f"[{s['heading']}] " if s["heading"] else ""
        text = prefix + s["text"]
        if len(text) <= max_len:
            chunks.append(text)
        else:
            start = 0
            while start < len(text):
                chunks.append(text[start:start + max_len])
                start += max_len - overlap
                if start >= len(text) - overlap:
                    break
    return chunks


# ── Indexing a single page ────────────────────────────────

def embed_chunks(db, page_id: str, title: str,
                 sections: list[dict], batch_size: int = 10) -> int:
    """对 sections 的 chunk 做 embedding 并写入数据库。返回 chunk 数。"""
    chunks = split_chunks(sections)
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        embeddings = get_embeddings(batch)
        for j, text in enumerate(batch):
            insert_chunk(db, page_id, title, i + j, text, embeddings[j])
    return len(chunks)


def index_page(db, page_id: str, title: str) -> int:
    """索引单个页面及其子页面。返回 chunk 总数。
    
    子页面标题会继承父页面的 section 名：美食 > 🧁甜点 > 提拉米苏
    这样搜索"甜点"就能找到所有甜品子页面。
    """
    blocks = call_tool("get_page_blocks", {"page_id": page_id})
    total_chunks = 0

    # 主页内容
    sections = extract_page_text(blocks)
    if sections:
        total_chunks += embed_chunks(db, page_id, title, sections)

    # 子页面：跟踪当前 section 名，让子页面标题带上下文
    current_section = ""
    sub_queue = []
    for b in blocks:
        btype = b.get("type", "")
        btext = extract_block_text(b) or ""
        if btype == "heading":
            current_section = btext
        elif btype == "page" and b["id"] != page_id and (b.get("children") or {}).get("ids"):
            # 子页面标题 = 父section名 > 子页面名
            sub_title = btext
            if current_section and current_section not in sub_title:
                sub_title = f"{current_section} > {sub_title}"
            sub_queue.append({"sub_id": b["id"], "sub_title": sub_title, "depth": 1})

    if sub_queue:
        total_chunks += index_sub_pages(db, page_id, title, sub_queue)

    if total_chunks > 0:
        set_index_meta(db, page_id, title, len(blocks))
    return total_chunks


def index_sub_pages(db, parent_id: str, parent_title: str,
                    queue: list[dict], max_depth: int = 10) -> int:
    """并发索引子页面。"""
    total_chunks = 0
    idx = 0

    while idx < len(queue):
        batch = queue[idx:idx + CONCURRENCY]
        idx += CONCURRENCY

        for item in batch:
            try:
                sub_id = item["sub_id"]
                sub_title = item["sub_title"]
                depth = item["depth"]
                if depth > max_depth:
                    continue

                sub_block = call_tool("get_block", {
                    "block_id": sub_id, "include_children": True
                })
                children = sub_block.get("children", [])
                if not children:
                    continue

                full_title = f"{parent_title} > {sub_title}" if sub_title else parent_title
                sub_sections = extract_page_text(children)
                if sub_sections:
                    total_chunks += embed_chunks(db, parent_id, full_title, sub_sections)

                # 发现更深层子页面
                for cb in children:
                    if cb.get("type") == "page" and cb["id"] != sub_id:
                        if (cb.get("children") or {}).get("ids"):
                            queue.append({
                                "sub_id": cb["id"],
                                "sub_title": extract_block_text(cb) or sub_title,
                                "depth": depth + 1
                            })
            except Exception as e:
                print(f"  SKIP sub-page {item.get('sub_id', '?')}: {e}")

    return total_chunks


# ── Page discovery ────────────────────────────────────────

def discover_all_pages() -> list[dict]:
    """发现所有需要索引的 Wolai 页面。"""
    all_pages = []
    visited = set()

    # 1. list_docs（API 限制 ~25 条）
    try:
        docs = call_tool("list_docs", {})
        for doc in docs:
            pid = doc.get("id") or doc.get("page_id")
            if pid:
                visited.add(pid)
                all_pages.append({"id": pid, "title": doc.get("title") or doc.get("name") or ""})
    except Exception as e:
        print(f"  list_docs: {e}")

    # 2. Dashboard (Claude)
    if DASHBOARD_ID not in visited:
        visited.add(DASHBOARD_ID)
        all_pages.append({"id": DASHBOARD_ID, "title": "Claude"})

    # 3. Dashboard 的父页面 (Library)
    try:
        dash_info = call_tool("get_doc", {"doc_id": DASHBOARD_ID})
        doc = dash_info.get("document", {})
        parent_id = doc.get("parent_id")
        parent_type = doc.get("parent_type")
        if parent_id and parent_type != "workspace" and parent_id not in visited:
            try:
                parent_doc = call_tool("get_doc", {"doc_id": parent_id})
                parent_title = "Library"
                content = (parent_doc.get("document") or {}).get("content") or []
                if content:
                    parent_title = content[0].get("title", "Library")
                visited.add(parent_id)
                all_pages.append({"id": parent_id, "title": parent_title})
            except Exception:
                pass
    except Exception:
        pass

    # 4. Dashboard 的直接子页面
    try:
        dash_blocks = call_tool("get_page_blocks", {"page_id": DASHBOARD_ID})
        for b in dash_blocks:
            if b.get("type") in ("page", "child_page"):
                pid = b["id"]
                if pid and pid not in visited:
                    visited.add(pid)
                    all_pages.append({"id": pid, "title": extract_block_text(b) or ""})
    except Exception as e:
        print(f"  dashboard: {e}")

    # 5. 已知页面补充
    EXTRA = [
        {"id": "4DatK4FC1eijQHSPq41qBP", "title": "软件"},
    ]
    for ep in EXTRA:
        if ep["id"] not in visited:
            visited.add(ep["id"])
            all_pages.append(ep)

    # 5.5 用 search_docs 补漏 — list_docs 只返回 25 条，大量页面会被截断
    # 从已知页面标题中自动提取高频字符作为搜索种子（语言无关，英文/中文/日文均适用）
    if len(visited) <= 50:
        # 收集已知标题中的字符频率，取 TOP 20 作为种子
        from collections import Counter
        char_freq = Counter()
        for p in all_pages:
            for ch in p.get("title", ""):
                if ch.strip() and not ch.isspace():
                    char_freq[ch] += 1
        seeds = [ch for ch, _ in char_freq.most_common(20)] if char_freq else list("aesito")
        print(f"  Search supplement: {len(seeds)} seed chars from existing titles ({len(visited)} known)...")
        import concurrent.futures
        def _search_one(ch):
            try:
                return call_tool("search_docs", {"query": ch, "title_only": True, "limit": 100})
            except Exception:
                return []
        found_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            for results in ex.map(_search_one, seeds):
                for r in results:
                    pid = r.get("id") or r.get("page_id")
                    title = r.get("title", "")
                    if pid and pid not in visited:
                        visited.add(pid)
                        all_pages.append({"id": pid, "title": title})
                        found_count += 1
        print(f"  Found {found_count} new pages, {len(visited)} total after supplement.")

    # 6. 扫描子页面
    to_scan = [p for p in all_pages if not p.get("_scanned")]
    for i in range(0, len(to_scan), CONCURRENCY):
        batch = to_scan[i:i + CONCURRENCY]
        for page in batch:
            page["_scanned"] = True
            try:
                blocks = call_tool("get_page_blocks", {"page_id": page["id"]})
                for b in blocks:
                    if b.get("type") in ("page", "child_page") and b["id"] not in visited:
                        visited.add(b["id"])
                        all_pages.append({"id": b["id"], "title": extract_block_text(b) or ""})
            except Exception:
                pass

    for p in all_pages:
        p.pop("_scanned", None)
    return all_pages


# ── Main ──────────────────────────────────────────────────

def main():
    if not TOKEN:
        print("ERROR: WOLAI_TOKEN not set. Create .env with WOLAI_TOKEN=...")
        sys.exit(1)

    db = get_db()

    # 确保 FTS5 有数据
    fts_cnt = db.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0]
    chunk_cnt = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunk_cnt > 0 and fts_cnt == 0:
        print(f"Backfilling {chunk_cnt} chunks into FTS5...")
        n = backfill_fts(db)
        print(f"  Backfilled {n} chunks.")

    print("Discovering pages...")
    all_pages = discover_all_pages()
    print(f"Found {len(all_pages)} pages total.")

    indexed = get_indexed_pages(db)

    to_index = []
    modified = []

    if not indexed:
        to_index = all_pages
    else:
        for page in all_pages:
            existing = indexed.get(page["id"])
            if not existing:
                to_index.append(page)
            elif len(indexed) <= 50:
                try:
                    info = call_tool("get_doc", {"doc_id": page["id"]})
                    doc = info.get("document", {})
                    edited_at = doc.get("edited_at", 0)
                    if edited_at > existing["indexed_at"] * 1000:
                        modified.append(page)
                except Exception:
                    pass
        if len(indexed) > 50:
            print(f"  (skipping edit-detection for {len(indexed)} already-indexed pages)")

    print(f"  {len(to_index)} new, {len(modified)} modified.")

    # 删除已修改页面的旧数据
    for page in modified:
        db.execute("DELETE FROM chunks WHERE page_id = ?", (page["id"],))
    db.commit()

    all_to_process = modified + to_index
    total_chunks = 0
    processed = 0

    for i in range(0, len(all_to_process), CONCURRENCY):
        batch = all_to_process[i:i + CONCURRENCY]
        for page in batch:
            try:
                chunks = index_page(db, page["id"], page["title"])
                total_chunks += chunks
                print(f"  {page['title'] or page['id']}... {chunks} chunks")
            except Exception as e:
                print(f"  {page['title'] or page['id']}... SKIP: {e}")
            processed += 1
            if processed % 20 == 0:
                db.commit()

    db.commit()

    pc = db.execute("SELECT COUNT(*) FROM index_meta").fetchone()[0]
    cc = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    fts_c = db.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0]
    fts_f = db.execute("SELECT COUNT(*) FROM fts_fuzzy").fetchone()[0]
    print(f"\nDone! {total_chunks} chunks across {pc} pages ({cc} total chunks in db).")
    print(f"FTS5: {fts_c} content entries, {fts_f} fuzzy entries.")


if __name__ == "__main__":
    main()

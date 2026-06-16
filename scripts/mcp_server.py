#!/usr/bin/env python3
"""Wolai Semantic Search — MCP Server (stdio JSON-RPC 2.0).

在 Claude Code 的 claude.json 中注册：
{
  "mcpServers": {
    "wolai-semantic-search": {
      "command": "python",
      "args": ["<路径>/scripts/mcp_server.py"]
    }
  }
}
"""
import json
import sys
import time
from pathlib import Path

# 把 scripts/ 父目录加入 path，以便 import db
sys.path.insert(0, str(Path(__file__).parent))
from db import get_db, hybrid_search, backfill_fts


def main():
    db = get_db()

    # 检测是否需要回填 FTS5 索引（旧数据没有 FTS5 记录）
    fts_count = db.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0]
    chunk_count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunk_count > 0 and fts_count == 0:
        print(f"[wolai-semantic-search] Backfilling {chunk_count} chunks into FTS5 index...", file=sys.stderr, flush=True)
        n = backfill_fts(db)
        print(f"[wolai-semantic-search] Backfilled {n} chunks into FTS5.", file=sys.stderr, flush=True)

    buffer = ""
    for line in sys.stdin:
        buffer += line
        # MCP 消息以换行分隔，但内容本身可能含换行
        # 尝试解析完整的 JSON-RPC 消息
        while "\n" in buffer:
            msg_line, buffer = buffer.split("\n", 1)
            msg_line = msg_line.strip()
            if not msg_line:
                continue
            try:
                msg = json.loads(msg_line)
                handle_message(db, msg)
            except json.JSONDecodeError:
                # 不完整消息，放回 buffer
                buffer = msg_line + "\n" + buffer
                break
            except Exception as e:
                print(f"[wolai-semantic-search] Error: {e}", file=sys.stderr, flush=True)

    # 处理最后残留
    if buffer.strip():
        try:
            msg = json.loads(buffer.strip())
            handle_message(db, msg)
        except Exception:
            pass


def handle_message(db, msg: dict):
    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    try:
        if method == "initialize":
            send_response(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "wolai-semantic-search",
                    "version": "2.0.0"
                }
            })

        elif method == "notifications/initialized":
            pass  # no response needed

        elif method == "tools/list":
            send_response(msg_id, {
                "tools": [
                    {
                        "name": "semantic_search",
                        "description": (
                            "混合搜索 Wolai 知识库（语义向量 + FTS5全文 + trigram模糊），"
                            "支持自然语言模糊描述查找笔记。三路结果用 RRF 算法融合排序。"
                            "模糊搜索容忍拼写错误（如 'obsidan' 可匹配 'obsidian'）。"
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "搜索关键词或自然语言模糊描述"
                                },
                                "limit": {
                                    "type": "number",
                                    "description": "返回结果数量，默认 5",
                                    "default": 5
                                },
                                "exclude_page_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "排除的 page_id 列表",
                                    "default": ["fBP45AGmSer4pw2qVsCPyF"]
                                }
                            },
                            "required": ["query"]
                        }
                    },
                    {
                        "name": "index_status",
                        "description": "查看搜索索引状态（已索引页面数、分块数、最后索引时间、FTS5 状态）",
                        "inputSchema": {
                            "type": "object",
                            "properties": {}
                        }
                    }
                ]
            })

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})

            if tool_name == "semantic_search":
                query = args.get("query", "")
                if not query:
                    raise ValueError("query is required")
                limit = args.get("limit", 5)
                exclude = args.get("exclude_page_ids", ["fBP45AGmSer4pw2qVsCPyF"])

                t0 = time.time()
                results = hybrid_search(db, query, limit, exclude)
                elapsed = round((time.time() - t0) * 1000)

                send_response(msg_id, {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "results": results,
                            "query": query,
                            "elapsed_ms": elapsed
                        }, ensure_ascii=False, indent=2)
                    }]
                })

            elif tool_name == "index_status":
                page_cnt = db.execute("SELECT COUNT(*) FROM index_meta").fetchone()[0]
                chunk_cnt = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                fts_content_cnt = db.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0]
                fts_fuzzy_cnt = db.execute("SELECT COUNT(*) FROM fts_fuzzy").fetchone()[0]

                last_row = db.execute(
                    "SELECT MAX(indexed_at) FROM index_meta"
                ).fetchone()
                last_t = last_row[0] if last_row else None

                send_response(msg_id, {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "indexed_pages": page_cnt,
                            "indexed_chunks": chunk_cnt,
                            "fts_content_entries": fts_content_cnt,
                            "fts_fuzzy_entries": fts_fuzzy_cnt,
                            "fts5_enabled": True,
                            "last_indexed_at": (
                                time.strftime("%Y-%m-%dT%H:%M:%S",
                                              time.localtime(last_t))
                                if last_t else None
                            )
                        }, ensure_ascii=False, indent=2)
                    }]
                })

            else:
                raise ValueError(f"Unknown tool: {tool_name}")

        else:
            # 未知 method，空响应
            if msg_id is not None:
                send_response(msg_id, {})

    except Exception as e:
        if msg_id is not None:
            send_error(msg_id, -32603, str(e))


def send_response(msg_id, result):
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": msg_id, "result": result
    }, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_error(msg_id, code, message):
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": msg_id,
        "error": {"code": code, "message": message}
    }, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

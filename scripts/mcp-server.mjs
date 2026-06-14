import { initDb, saveDb, hybridSearch } from "./db.mjs";

// Minimal stdio MCP server implementing JSON-RPC 2.0

let db;

async function main() {
  db = await initDb();

  let buffer = "";
  process.stdin.setEncoding("utf-8");
  process.stdin.on("data", async (chunk) => {
    buffer += chunk;
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        await handleMessage(JSON.parse(trimmed));
      } catch {}
    }
  });

  process.stdin.on("end", () => {
    if (buffer.trim()) {
      try { handleMessage(JSON.parse(buffer.trim())); } catch {}
    }
  });
}

async function handleMessage(msg) {
  const { id, method, params } = msg;

  try {
    switch (method) {
      case "initialize":
        sendResponse(id, {
          protocolVersion: "2024-11-05",
          capabilities: {
            tools: {},  // server supports tools — actual definitions in tools/list
          },
          serverInfo: {
            name: "wolai-semantic-search",
            version: "1.0.0",
          },
        });
        break;

      case "notifications/initialized":
        break;

      case "tools/list":
        sendResponse(id, {
          tools: [
            {
              name: "semantic_search",
              description: "混合搜索 Wolai 知识库（语义+全文+关键词），支持模糊描述查找笔记",
              inputSchema: {
                type: "object",
                properties: {
                  query: { type: "string", description: "搜索关键词或模糊描述" },
                  limit: { type: "number", description: "返回结果数量，默认 5", default: 5 },
                },
                required: ["query"],
              },
            },
            {
              name: "index_status",
              description: "查看搜索索引状态（已索引页面数、分块数、最后索引时间）",
              inputSchema: { type: "object", properties: {} },
            },
          ],
        });
        break;

      case "tools/call":
        const toolName = params?.name;
        const args = params?.arguments || {};

        if (toolName === "semantic_search") {
          if (!args.query) throw new Error("query is required");
          const excludePages = args.exclude_page_ids || ["fBP45AGmSer4pw2qVsCPyF"]; // 默认排除"网页"
          const results = await hybridSearch(args.query, args.limit || 5, excludePages);
          sendResponse(id, {
            content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
          });
        } else if (toolName === "index_status") {
          const pageCnt = db.exec("SELECT COUNT(*) FROM index_meta")[0].values[0][0];
          const chunkCnt = db.exec("SELECT COUNT(*) FROM chunks")[0].values[0][0];
          const lastRows = db.exec("SELECT MAX(indexed_at) FROM index_meta");
          const lastT = lastRows[0]?.values[0]?.[0] || null;
          sendResponse(id, {
            content: [{
              type: "text",
              text: JSON.stringify({
                indexed_pages: pageCnt,
                indexed_chunks: chunkCnt,
                last_indexed_at: lastT ? new Date(lastT * 1000).toISOString() : null,
              }, null, 2),
            }],
          });
        } else {
          throw new Error(`Unknown tool: ${toolName}`);
        }
        break;

      default:
        if (id) sendResponse(id, {});
    }
  } catch (e) {
    if (id) sendError(id, e.code || -32603, e.message || "Internal error");
  }
}

function sendResponse(id, result) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
}

function sendError(id, code, message) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n");
}

main().catch(e => {
  console.error("FATAL:", e);
  process.exit(1);
});

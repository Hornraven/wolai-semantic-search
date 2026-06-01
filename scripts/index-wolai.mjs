import { initDb, saveDb, getEmbeddings, insertChunk, setIndexMeta, getIndexedPages } from "./db.mjs";
import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

function loadToken() {
  try {
    const __dir = dirname(fileURLToPath(import.meta.url));
    const envPath = join(__dir, "..", ".env");
    const env = readFileSync(envPath, "utf-8");
    const m = env.match(/WOLAI_TOKEN=(.+)/);
    if (m) return m[1].trim();
  } catch {}
  return process.env.WOLAI_TOKEN || "";
}
const TOKEN = loadToken();
const API_URL = "https://api.wolai.com/v1/mcp/";

let reqId = 1;
function nextId() { return reqId++; }

async function wolaiCall(method, params) {
  const resp = await fetch(API_URL, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + TOKEN,
      "Content-Type": "application/json",
      Accept: "text/event-stream, application/json",
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: nextId(), method, params }),
  });
  const text = await resp.text();
  const lines = text.split("\n").filter(l => l.startsWith("data: ")).map(l => JSON.parse(l.slice(6)));
  return lines;
}

async function callTool(name, args) {
  const result = await wolaiCall("tools/call", { name, arguments: args });
  const r = result[0]?.result;
  if (!r) throw new Error(`Tool ${name} failed: ${JSON.stringify(result)}`);
  const content = r.content?.[0];
  if (r.isError) throw new Error(`Tool ${name} error: ${content?.text}`);
  const parsed = JSON.parse(content?.text || "{}");
  if (parsed.data?.data) return parsed.data.data;   // list_docs, get_page_blocks
  if (parsed.data?.children) return parsed.data;    // get_block → {block, children}
  return parsed;
}

function extractBlockText(block) {
  // simple_table: data lives in table_content (2D array of cells)
  if (block.type === "simple_table" && block.table_content) {
    return block.table_content.map(cells =>
      cells.map(cell => (cell.content || []).map(c => c.title || "").join(" ")).join(" | ")
    ).join("\n").trim();
  }

  // file: filename is in caption, not content
  if (block.type === "file" && block.caption) {
    return block.caption.trim();
  }

  const content = block.content;
  if (!content || !Array.isArray(content)) {
    // fallback: some blocks have caption instead of content
    if (block.caption) return block.caption.trim();
    return "";
  }
  const isCode = ["code", "equation", "block_equation"].includes(block.type);
  return content
    .map(c => {
      if (typeof c === "string") return c;
      if (c.type === "bi_link") return c.title;
      return c.title || "";
    })
    .join(isCode ? "\n" : "")
    .trim();
}

function extractPageText(blocks) {
  const sections = [];
  let currentHeading = "";
  let currentTexts = [];

  function flush() {
    const text = currentTexts.join("\n").trim();
    if (text.length > 5) sections.push({ heading: currentHeading, text });
    currentTexts = [];
  }

  for (const block of blocks) {
    const text = extractBlockText(block);
    if (!text) continue;

    if (block.type === "heading") {
      flush();
      currentHeading = text;
    } else if (["text", "bull_list", "enum_list", "todo_list", "code", "quote", "callout", "toggle_list", "simple_table", "database", "template_button", "file"].includes(block.type)) {
      currentTexts.push(text);
    }
  }
  flush();
  return sections;
}

function splitChunks(sections, maxLen = 500, overlap = 64) {
  const chunks = [];
  for (const s of sections) {
    const prefix = s.heading ? `[${s.heading}] ` : "";
    const text = prefix + s.text;
    if (text.length <= maxLen) {
      chunks.push(text);
    } else {
      let start = 0;
      while (start < text.length) {
        chunks.push(text.slice(start, Math.min(start + maxLen, text.length)));
        start = Math.min(start + maxLen - overlap, text.length);
        if (start >= text.length - overlap) break;
      }
    }
  }
  return chunks;
}

async function indexPage(pageId, title) {
  const blocks = await callTool("get_page_blocks", { page_id: pageId });
  let totalChunks = 0;

  // Index main page content
  const sections = extractPageText(blocks);
  if (sections.length > 0) {
    totalChunks += await embedChunks(pageId, title, sections);
  }

  // Recursively index sub-pages (type=page blocks in get_page_blocks).
  // Uses get_page_blocks output (not get_doc children.ids) to correctly
  // identify actual sub-pages vs content blocks.
  const subQueue = blocks
    .filter(b => b.type === "page" && b.id !== pageId && b.children?.ids?.length > 0)
    .map(b => ({ subId: b.id, subTitle: extractBlockText(b) || "", depth: 1 }));

  if (subQueue.length > 0) {
    totalChunks += await expandSubPagesConcurrent(pageId, title, subQueue);
  }

  if (totalChunks > 0) setIndexMeta(pageId, title, blocks.length);
  return totalChunks;
}

async function expandSubPagesConcurrent(parentId, parentTitle, queue, maxDepth = 10, concurrency = 5) {
  let totalChunks = 0;
  let idx = 0;

  while (idx < queue.length) {
    const batch = queue.slice(idx, idx + concurrency);
    idx += concurrency;

    const results = await Promise.allSettled(batch.map(async item => {
      const { subId, subTitle, depth } = item;
      if (depth > maxDepth) return 0;

      const subBlock = await callTool("get_block", { block_id: subId, include_children: true });
      if (!subBlock?.children?.length) return 0;

      const fullTitle = subTitle ? `${parentTitle} > ${subTitle}` : parentTitle;
      const subSections = extractPageText(subBlock.children);
      let chunks = 0;
      if (subSections.length > 0) {
        chunks = await embedChunks(parentId, fullTitle, subSections);
      }

      // Discover deeper sub-pages
      for (const cb of subBlock.children) {
        if (cb.type === "page" && cb.id !== subId && cb.children?.ids?.length > 0) {
          queue.push({ subId: cb.id, subTitle: extractBlockText(cb) || subTitle, depth: depth + 1 });
        }
      }
      return chunks;
    }));

    for (const r of results) {
      if (r.status === "fulfilled") totalChunks += r.value;
    }
  }

  return totalChunks;
}

async function embedChunks(pageId, title, sections) {
  const chunks = splitChunks(sections);
  for (let i = 0; i < chunks.length; i += 10) {
    const batch = chunks.slice(i, i + 10);
    const embeddings = await getEmbeddings(batch);
    for (let j = 0; j < batch.length; j++) {
      insertChunk(pageId, title, i + j, batch[j], embeddings[j]);
    }
  }
  return chunks.length;
}

// --- Manual page discovery via list_docs + direct sub-page listing ---

async function discoverAllPages() {
  const allPages = [];
  const visited = new Set();

  // 1. List all top-level docs (API caps at ~25)
  try {
    const docs = await callTool("list_docs", {});
    for (const doc of docs) {
      const pid = doc.id || doc.page_id;
      if (pid) {
        visited.add(pid);
        allPages.push({ id: pid, title: doc.title || doc.name || "" });
      }
    }
  } catch (e) {
    console.warn("list_docs:", e.message);
  }

  // 2. Dashboard (Claude) page
  const DASHBOARD_ID = "kKEsWsXDv2KBEFHWqse15a";
  if (!visited.has(DASHBOARD_ID)) {
    visited.add(DASHBOARD_ID);
    allPages.push({ id: DASHBOARD_ID, title: "Claude" });
  }

  // Also discover Claude's parent (Library) — it's a top-level page
  // not returned by list_docs (25 page API cap).
  try {
    const dashInfo = await callTool("get_doc", { doc_id: DASHBOARD_ID });
    const parentId = dashInfo?.data?.document?.parent_id;
    const parentType = dashInfo?.data?.document?.parent_type;
    if (parentId && parentType !== "workspace" && !visited.has(parentId)) {
      try {
        const parentDoc = await callTool("get_doc", { doc_id: parentId });
        const parentTitle = parentDoc?.data?.document?.content?.[0]?.title || "(Library)";
        visited.add(parentId);
        allPages.push({ id: parentId, title: parentTitle });
      } catch {}
    }
  } catch {}

  // 3. Check dashboard's page blocks for sub-pages
  try {
    const dashboardBlocks = await callTool("get_page_blocks", { page_id: DASHBOARD_ID });
    for (const b of dashboardBlocks) {
      if (b.type === "page" || b.type === "child_page") {
        const pid = b.id;
        if (pid && !visited.has(pid)) {
          visited.add(pid);
          allPages.push({ id: pid, title: extractBlockText(b) || "" });
        }
      }
    }
  } catch (e) {
    console.warn("dashboard:", e.message);
  }

  // 4. Known extra workspace-level pages not returned by list_docs (API cap)
  const EXTRA_PAGES = [
    { id: "4DatK4FC1eijQHSPq41qBP", title: "软件" },
  ];
  for (const ep of EXTRA_PAGES) {
    if (!visited.has(ep.id)) {
      visited.add(ep.id);
      allPages.push(ep);
    }
  }

  // 5. Scan discovered pages for type=page sub-pages (one pass, concurrent).
  //    Further nesting is handled by expandSubPagesConcurrent during indexing.
  const toScan = allPages.filter(p => !p._scanned);
  const DISCOVERY_CONCURRENCY = 5;
  for (let i = 0; i < toScan.length; i += DISCOVERY_CONCURRENCY) {
    const batch = toScan.slice(i, i + DISCOVERY_CONCURRENCY);
    const scanResults = await Promise.allSettled(batch.map(async page => {
      page._scanned = true;
      try {
        const blocks = await callTool("get_page_blocks", { page_id: page.id });
        return blocks
          .filter(b => (b.type === "page" || b.type === "child_page") && b.id && !visited.has(b.id))
          .map(b => {
            visited.add(b.id);
            return { id: b.id, title: extractBlockText(b) || "" };
          });
      } catch {
        return [];
      }
    }));
    for (const r of scanResults) {
      if (r.status === "fulfilled") {
        for (const np of r.value) allPages.push(np);
      }
    }
  }

  // Clean up internal _scanned flag
  for (const page of allPages) delete page._scanned;

  return allPages;
}

// --- Main ---

let db;
async function main() {
  db = await initDb();

  console.log("Discovering pages...");
  const allPages = await discoverAllPages();
  console.log(`Found ${allPages.length} pages total.`);

  const indexed = getIndexedPages();
  const CONCURRENCY = 5;

  // Separate new pages from existing ones.  For existing pages, check
  // edited_at to detect changes (only when fewer than 50 — the common
  // incremental case — otherwise skip the check to keep it fast).
  const toIndex = [];
  let modified = [];
  let skipped = 0;

  if (indexed.size === 0) {
    // First run: index everything
    toIndex.push(...allPages);
  } else {
    for (const page of allPages) {
      const existing = indexed.get(page.id);
      if (!existing) {
        toIndex.push(page);
      } else if (indexed.size <= 50) {
        // Small DB: check for modifications
        try {
          const info = await callTool("get_doc", { doc_id: page.id });
          const editedAt = info?.data?.document?.edited_at || 0;
          if (editedAt > existing.indexed_at * 1000) {
            modified.push(page);
          }
        } catch {}
      }
      // else: large DB, skip edit-checking to stay fast
    }
    if (indexed.size > 50) {
      console.log(`  (skipping edit-detection for ${indexed.size} already-indexed pages)`);
    }
  }

  console.log(`  ${toIndex.length} new, ${modified.length} modified, ${skipped} skipped.`);

  let totalChunks = 0;
  let processed = 0;

  async function processBatch(pages) {
    for (let i = 0; i < pages.length; i += CONCURRENCY) {
      const batch = pages.slice(i, i + CONCURRENCY);
      const results = await Promise.allSettled(batch.map(async page => {
        try {
          const chunks = await indexPage(page.id, page.title);
          return { page, chunks };
        } catch (e) {
          return { page, chunks: 0, error: e.message };
        }
      }));
      for (const r of results) {
        if (r.status === "fulfilled") {
          const { page, chunks, error } = r.value;
          process.stdout.write(`  ${page.title || page.id}... `);
          if (error) {
            console.log(`SKIP: ${error}`);
          } else {
            totalChunks += chunks;
            console.log(`${chunks} chunks`);
          }
        }
      }
      processed += batch.length;
      // Save every 20 pages to avoid losing progress on interrupt
      if (processed % 20 === 0) saveDb();
    }
  }

  // Re-index modified pages: delete their old chunks first
  for (const page of modified) {
    db.run("DELETE FROM chunks WHERE page_id = ?", [page.id]);
  }
  await processBatch(modified.concat(toIndex));

  saveDb();
  const pc = db.exec("SELECT COUNT(*) FROM index_meta")[0].values[0][0];
  const cc = db.exec("SELECT COUNT(*) FROM chunks")[0].values[0][0];
  console.log(`\nDone! ${totalChunks} chunks across ${pc} pages (${cc} total chunks in db).`);
}

main().catch(e => { console.error(e); process.exit(1); });

import initSqlJs from "sql.js";
import { join, dirname } from "path";
import { existsSync, readFileSync, writeFileSync } from "fs";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DB_PATH = join(__dirname, "..", "data", "vectors.db");

// --- SQLite (sql.js, WASM-based, no native bindings) ---

let db;
let SQL;

export async function initDb() {
  if (db) return db;
  SQL = await initSqlJs();

  if (existsSync(DB_PATH)) {
    const buffer = readFileSync(DB_PATH);
    db = new SQL.Database(buffer);
  } else {
    db = new SQL.Database();
  }

  initSchema();
  return db;
}

function initSchema() {
  db.run(`
    CREATE TABLE IF NOT EXISTS chunks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      page_id TEXT NOT NULL,
      title TEXT NOT NULL,
      chunk_index INTEGER NOT NULL,
      chunk_text TEXT NOT NULL,
      embedding TEXT,
      updated_at INTEGER NOT NULL
    )
  `);
  db.run("CREATE INDEX IF NOT EXISTS idx_chunks_page_id ON chunks(page_id)");

  db.run(`
    CREATE TABLE IF NOT EXISTS index_meta (
      page_id TEXT PRIMARY KEY,
      title TEXT,
      block_count INTEGER,
      indexed_at INTEGER NOT NULL
    )
  `);
}

function save() {
  const data = db.export();
  writeFileSync(DB_PATH, Buffer.from(data));
}

export function saveDb() { save(); }

// --- Embedding helpers ---

const OLLAMA_URL = "http://127.0.0.1:11434/api/embed";

export async function getEmbedding(text) {
  const resp = await fetch(OLLAMA_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "nomic-embed-text", input: text }),
  });
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(`Ollama error (${resp.status}): ${t.slice(0, 200)}`);
  }
  const data = await resp.json();
  return data.embeddings[0];
}

export async function getEmbeddings(batch) {
  const resp = await fetch(OLLAMA_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "nomic-embed-text", input: batch }),
  });
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(`Ollama error (${resp.status}): ${t.slice(0, 200)}`);
  }
  const data = await resp.json();
  return data.embeddings;
}

// --- Cosine similarity ---

function cosineSimilarity(a, b) {
  let dot = 0, magA = 0, magB = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    magA += a[i] * a[i];
    magB += b[i] * b[i];
  }
  if (magA === 0 || magB === 0) return 0;
  return dot / (Math.sqrt(magA) * Math.sqrt(magB));
}

// --- Indexing ---

export function insertChunk(pageId, title, chunkIndex, chunkText, embedding) {
  const text = chunkText.replace(/\0/g, ""); // FTS5 doesn't like null bytes
  db.run(
    "INSERT INTO chunks (page_id, title, chunk_index, chunk_text, embedding, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
    [pageId, title, chunkIndex, text, JSON.stringify(embedding), Math.floor(Date.now() / 1000)]
  );
  const id = db.exec("SELECT last_insert_rowid()")[0].values[0][0];
  return id;
}

export function setIndexMeta(pageId, title, blockCount) {
  const now = Math.floor(Date.now() / 1000);
  db.run(
    "INSERT OR REPLACE INTO index_meta (page_id, title, block_count, indexed_at) VALUES (?, ?, ?, ?)",
    [pageId, title, blockCount, now]
  );
}

export function getIndexedPages() {
  const rows = db.exec("SELECT page_id, title, indexed_at FROM index_meta");
  const map = new Map();
  if (rows.length > 0) {
    for (const row of rows[0].values) {
      map.set(row[0], { page_id: row[0], title: row[1], indexed_at: row[2] });
    }
  }
  return map;
}

// --- Hybrid search ---

function semanticSearch(queryVec, limit = 10) {
  const rows = db.exec(
    "SELECT id, page_id, title, chunk_index, chunk_text, embedding FROM chunks WHERE embedding IS NOT NULL"
  );
  if (rows.length === 0) return [];

  const scored = rows[0].values.map(row => ({
    id: row[0],
    page_id: row[1],
    title: row[2],
    chunk_index: row[3],
    chunk_text: row[4],
    score: cosineSimilarity(queryVec, JSON.parse(row[5])),
  }));
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, limit).map((r, i) => ({ ...r, rank: i + 1 }));
}

function keywordSearch(query, limit = 10) {
  const sanitized = query.replace(/[^\w一-鿿\s]/g, "").trim().toLowerCase();
  if (!sanitized) return [];

  const terms = sanitized.split(/\s+/).filter(t => t.length > 0);

  try {
    const rows = db.exec(
      "SELECT id, page_id, title, chunk_index, chunk_text FROM chunks"
    );
    if (rows.length === 0 || rows[0].values.length === 0) return [];

    const scored = [];
    for (const row of rows[0].values) {
      const text = (row[4] || "").toLowerCase();
      let score = 0;
      for (const term of terms) {
        let idx = 0, count = 0;
        while ((idx = text.indexOf(term, idx)) !== -1) {
          count++;
          idx += term.length;
        }
        score += count * (term.length > 1 ? term.length : 0.5);
      }
      if (score > 0) {
        scored.push({
          id: row[0],
          page_id: row[1],
          title: row[2],
          chunk_index: row[3],
          chunk_text: row[4],
          score,
        });
      }
    }

    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, limit).map((r, i) => ({ ...r, rank: i + 1 }));
  } catch {
    return [];
  }
}

export async function hybridSearch(query, limit = 5, excludePageIds = []) {
  const kSem = 60;  // semantic RRF constant
  const kKey = 20;  // keyword RRF constant (smaller = higher weight)

  // 1. Semantic
  let semanticResults = [];
  try {
    const qVec = await getEmbedding(query);
    semanticResults = semanticSearch(qVec, 10);
  } catch (e) {
    console.warn("Semantic search failed:", e.message);
  }

  // 2. Keyword
  const keywordResults = keywordSearch(query, 10);

  // 3. RRF merge (keyword weighted higher)
  const combined = new Map();

  for (const r of semanticResults) {
    combined.set(r.id, { ...r, rrfScore: 1 / (kSem + r.rank) });
  }
  for (const r of keywordResults) {
    if (combined.has(r.id)) {
      combined.get(r.id).rrfScore += 1 / (kKey + r.rank);
    } else {
      combined.set(r.id, { ...r, rrfScore: 1 / (kKey + r.rank) });
    }
  }

  const results = Array.from(combined.values());
  results.sort((a, b) => b.rrfScore - a.rrfScore);

  const excludeSet = new Set(excludePageIds);
  return results
    .filter(r => !excludeSet.has(r.page_id))
    .slice(0, limit)
    .map(r => ({
      page_id: r.page_id,
      title: r.title,
      snippet: truncateText(r.chunk_text, 150),
      score: Math.round(r.rrfScore * 1000) / 1000,
    }));
}

function truncateText(text, maxLen) {
  if (!text) return "";
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + "...";
}

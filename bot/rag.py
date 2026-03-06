"""RAG v4 — Chunk-based semantic search over conversations.

Architecture:
- 7-message sliding window chunks (stride 3) over all conversations
- Gemini gemini-embedding-001 embeddings (3072-dim, stored as float32 blobs)
- Cosine similarity search returns chunk text + msg ID ranges for drill-down
- Only skips msg_type in ('status', 'placeholder', 'system')
- Keeps everything else: short messages, emoji, "ok", media descriptors
- Post-search: agent can fetch full conversation context via msg ID range
"""

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
import numpy as np

from config import (
    GEMINI_API_KEY, DB_PATH, FAMILY_TIMEZONE,
    RAG_CHUNK_SIZE, RAG_CHUNK_STRIDE,
    RAG_EMBEDDING_MODEL, RAG_EMBEDDING_DIM, RAG_EMBEDDING_BATCH_SIZE,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo(FAMILY_TIMEZONE)

# === CONFIG (imported from config.py — single source of truth) ===
EMBEDDING_MODEL = RAG_EMBEDDING_MODEL
EMBEDDING_DIM = RAG_EMBEDDING_DIM
BATCH_SIZE = RAG_EMBEDDING_BATCH_SIZE
CHUNK_SIZE = RAG_CHUNK_SIZE
CHUNK_STRIDE = RAG_CHUNK_STRIDE
SKIP_MSG_TYPES = {"status", "placeholder", "system"}


# === EMBEDDING GENERATION ===

def _get_genai_client():
    """Lazy-init Gemini client."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured")
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _vec_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


async def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed a batch of texts via Gemini API. Returns list of embedding vectors."""
    if not texts:
        return []
    client = _get_genai_client()
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        try:
            result = await asyncio.to_thread(
                client.models.embed_content,
                model=EMBEDDING_MODEL,
                contents=batch,
            )
            for emb in result.embeddings:
                all_embeddings.append(np.array(emb.values, dtype=np.float32))
        except Exception as e:
            log.error(f"Embedding API error (batch {i // BATCH_SIZE}): {e}")
            for _ in batch:
                all_embeddings.append(np.zeros(EMBEDDING_DIM, dtype=np.float32))
    return all_embeddings


# === TABLE INIT ===

async def init_rag_tables():
    """Create rag_chunks table if it doesn't exist."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                start_msg_id INTEGER NOT NULL,
                end_msg_id INTEGER NOT NULL,
                senders TEXT NOT NULL,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                model TEXT NOT NULL DEFAULT 'gemini-embedding-001',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_msg_range
                ON rag_chunks(start_msg_id, end_msg_id);
        """)
        await db.commit()
    log.info("RAG v4 rag_chunks table initialized")


# === CHUNKING ===

def _format_message_for_chunk(row: dict) -> str:
    """Format a single message row into chunk text line."""
    source = row.get("source", "")
    user = row.get("user_name", "")
    text = row.get("text", "")
    ts = row.get("timestamp", "")[:16]  # trim seconds
    prefix = f"[{ts} {source}]"
    role = row.get("role", "")
    if role == "assistant":
        return f"{prefix} Bot: {text}"
    return f"{prefix} {user}: {text}"


async def _fetch_eligible_messages(after_msg_id: int = 0) -> list[dict]:
    """Fetch all messages eligible for chunking (skipping status/placeholder/system).

    Since msg_type column may not exist yet, we filter by role heuristic:
    - role='user' or role='assistant' → always include
    - We skip nothing by role alone; msg_type filtering added when column exists.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Check if msg_type column exists
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        has_msg_type = "msg_type" in columns

        if has_msg_type:
            cursor = await db.execute(
                "SELECT id, source, user_name, text, role, timestamp, msg_type "
                "FROM messages WHERE id > ? "
                "AND (msg_type IS NULL OR msg_type NOT IN ('status', 'placeholder', 'system')) "
                "ORDER BY id",
                (after_msg_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, source, user_name, text, role, timestamp "
                "FROM messages WHERE id > ? ORDER BY id",
                (after_msg_id,),
            )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


def _build_chunks(messages: list[dict]) -> list[dict]:
    """Build 7-message sliding window chunks with stride 3.

    Returns list of chunk dicts with:
        source, start_msg_id, end_msg_id, senders, start_ts, end_ts, chunk_text
    """
    chunks = []
    n = len(messages)
    if n == 0:
        return chunks

    i = 0
    while i < n:
        window = messages[i:i + CHUNK_SIZE]
        if not window:
            break

        # Build chunk text
        lines = [_format_message_for_chunk(m) for m in window]
        chunk_text = "\n".join(lines)

        # Extract metadata
        senders = sorted(set(m.get("user_name", "") for m in window if m.get("user_name")))
        sources = sorted(set(m.get("source", "") for m in window if m.get("source")))

        chunk = {
            "source": ",".join(sources),
            "start_msg_id": window[0]["id"],
            "end_msg_id": window[-1]["id"],
            "senders": ",".join(senders),
            "start_ts": window[0].get("timestamp", ""),
            "end_ts": window[-1].get("timestamp", ""),
            "chunk_text": chunk_text,
        }
        chunks.append(chunk)

        # Stride forward
        if i + CHUNK_SIZE >= n:
            break  # last window reached end
        i += CHUNK_STRIDE

    return chunks


# === BACKFILL ===

async def backfill_chunks(progress_callback=None) -> dict:
    """Build and embed all chunks from scratch.

    Drops existing chunks and rebuilds. Safe to run multiple times.
    Returns stats dict.
    """
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY not configured"}

    await init_rag_tables()

    t0 = time.time()

    # Fetch all eligible messages
    messages = await _fetch_eligible_messages(after_msg_id=0)
    total_msgs = len(messages)
    log.info(f"RAG backfill: {total_msgs} eligible messages")

    if total_msgs == 0:
        return {"total_messages": 0, "chunks_created": 0, "time_sec": 0}

    # Build chunks
    chunks = _build_chunks(messages)
    total_chunks = len(chunks)
    log.info(f"RAG backfill: {total_chunks} chunks to embed")

    if total_chunks == 0:
        return {"total_messages": total_msgs, "chunks_created": 0, "time_sec": 0}

    # Clear old chunks
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM rag_chunks")
        await db.commit()

    # Embed in batches and store
    embedded = 0
    errors = 0
    now = datetime.now(TZ).isoformat()

    for batch_start in range(0, total_chunks, BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        texts = [c["chunk_text"][:2000] for c in batch]  # cap embedding input

        try:
            embeddings = await embed_texts(texts)

            async with aiosqlite.connect(str(DB_PATH)) as db:
                for chunk, emb in zip(batch, embeddings):
                    if np.allclose(emb, 0):
                        errors += 1
                        continue
                    await db.execute(
                        "INSERT INTO rag_chunks "
                        "(source, start_msg_id, end_msg_id, senders, start_ts, end_ts, "
                        "chunk_text, embedding, model, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (chunk["source"], chunk["start_msg_id"], chunk["end_msg_id"],
                         chunk["senders"], chunk["start_ts"], chunk["end_ts"],
                         chunk["chunk_text"], _vec_to_blob(emb), EMBEDDING_MODEL, now),
                    )
                    embedded += 1
                await db.commit()

            if progress_callback:
                await progress_callback(batch_start + len(batch), total_chunks, embedded)

            # Rate limit: be gentle with Gemini API
            if batch_start + BATCH_SIZE < total_chunks:
                await asyncio.sleep(0.5)

        except Exception as e:
            log.error(f"RAG backfill batch error at {batch_start}: {e}", exc_info=True)
            errors += len(batch)

    elapsed = round(time.time() - t0, 1)
    stats = {
        "total_messages": total_msgs,
        "chunks_created": embedded,
        "chunks_failed": errors,
        "time_sec": elapsed,
    }
    log.info(f"RAG backfill complete: {stats}")
    return stats


# === INCREMENTAL UPDATE ===

async def update_chunks_incremental():
    """Add chunks for new messages since the last chunk.

    Called periodically or after new messages arrive.
    Finds the last chunked message ID and builds new chunks from there.
    """
    if not GEMINI_API_KEY:
        return

    await init_rag_tables()

    # Find the highest end_msg_id in existing chunks
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT MAX(end_msg_id) FROM rag_chunks")
        row = await cursor.fetchone()
        last_chunked = row[0] if row and row[0] else 0

    # We need some overlap for context continuity — go back by CHUNK_SIZE msgs
    overlap_start = max(0, last_chunked - CHUNK_SIZE + 1)
    messages = await _fetch_eligible_messages(after_msg_id=overlap_start)

    if len(messages) < CHUNK_SIZE:
        return  # not enough new messages for a full chunk

    chunks = _build_chunks(messages)
    if not chunks:
        return

    # Filter out chunks that overlap with existing ones
    # Only keep chunks where start_msg_id > last_chunked - CHUNK_STRIDE
    new_chunks = [c for c in chunks if c["end_msg_id"] > last_chunked]
    if not new_chunks:
        return

    # Embed and store
    texts = [c["chunk_text"][:2000] for c in new_chunks]
    try:
        embeddings = await embed_texts(texts)
    except Exception as e:
        log.error(f"RAG incremental embed failed: {e}")
        return

    now = datetime.now(TZ).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        for chunk, emb in zip(new_chunks, embeddings):
            if np.allclose(emb, 0):
                continue
            await db.execute(
                "INSERT INTO rag_chunks "
                "(source, start_msg_id, end_msg_id, senders, start_ts, end_ts, "
                "chunk_text, embedding, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk["source"], chunk["start_msg_id"], chunk["end_msg_id"],
                 chunk["senders"], chunk["start_ts"], chunk["end_ts"],
                 chunk["chunk_text"], _vec_to_blob(emb), EMBEDDING_MODEL, now),
            )
        await db.commit()

    log.info(f"RAG incremental: added {len(new_chunks)} new chunks")


# === SEARCH ===

async def rag_search(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over chunk embeddings.

    Returns list of dicts:
        chunk_text, similarity, source, senders, start_ts, end_ts,
        start_msg_id, end_msg_id
    Sorted by similarity descending.
    """
    if not GEMINI_API_KEY:
        return []

    # Embed query
    try:
        embeddings = await embed_texts([query])
        if not embeddings or np.allclose(embeddings[0], 0):
            return []
        query_vec = embeddings[0]
    except Exception as e:
        log.error(f"RAG search embed failed: {e}")
        return []

    # Load all chunk embeddings
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT chunk_id, source, start_msg_id, end_msg_id, senders, "
            "start_ts, end_ts, chunk_text, embedding FROM rag_chunks"
        )
        rows = await cursor.fetchall()

    if not rows:
        return []

    # Build matrix and compute cosine similarity
    chunk_data = []
    emb_list = []
    for row in rows:
        try:
            vec = _blob_to_vec(row["embedding"])
            if len(vec) == EMBEDDING_DIM:
                emb_list.append(vec)
                chunk_data.append(dict(row))
        except Exception:
            continue

    if not emb_list:
        return []

    matrix = np.stack(emb_list)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
    similarities = (matrix / norms) @ query_norm

    # Top-K
    k = min(top_k, len(chunk_data))
    top_indices = np.argsort(similarities)[-k:][::-1]

    results = []
    for idx in top_indices:
        score = float(similarities[idx])
        if score < 0.25:  # minimum relevance threshold
            continue
        cd = chunk_data[idx]
        results.append({
            "chunk_text": cd["chunk_text"],
            "similarity": round(score, 4),
            "source": cd["source"],
            "senders": cd["senders"],
            "start_ts": cd["start_ts"],
            "end_ts": cd["end_ts"],
            "start_msg_id": cd["start_msg_id"],
            "end_msg_id": cd["end_msg_id"],
        })

    return results


# === STATS ===

async def get_rag_stats() -> dict:
    """Get RAG chunk coverage stats."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM messages")
        total_messages = (await cursor.fetchone())[0]

        try:
            cursor = await db.execute("SELECT COUNT(*) FROM rag_chunks")
            total_chunks = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT MIN(start_msg_id), MAX(end_msg_id) FROM rag_chunks")
            row = await cursor.fetchone()
            min_id, max_id = row if row else (None, None)
        except Exception:
            total_chunks = 0
            min_id, max_id = None, None

    return {
        "total_messages": total_messages,
        "total_chunks": total_chunks,
        "msg_range": f"{min_id}-{max_id}" if min_id else "none",
    }

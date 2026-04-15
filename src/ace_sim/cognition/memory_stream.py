from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np


class EmbeddingProvider(Protocol):
    vector_dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


@dataclass
class MemoryRecord:
    record_id: int
    agent_id: str
    text: str
    channel: str
    tick: int
    importance: float
    metadata: dict[str, Any]


class LocalSentenceTransformerEmbedder:
    """Local-only embedding provider. No remote embedding API calls."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        vector_dim: int = 384,
    ) -> None:
        self.model_name = model_name
        self.vector_dim = int(vector_dim)
        self._model = None
        self._model_error: str | None = None
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name)
        except Exception as exc:  # noqa: BLE001
            self._model = None
            self._model_error = str(exc)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.vector_dim), dtype=np.float32)

        if self._model is not None:
            vectors = self._model.encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.ndim == 1:
                vectors = vectors.reshape(1, -1)
            return self._fit_or_pad(vectors)

        # Deterministic local fallback when sentence-transformers is unavailable.
        return np.vstack([self._hash_embedding(text) for text in texts]).astype(np.float32)

    def _fit_or_pad(self, vectors: np.ndarray) -> np.ndarray:
        if vectors.shape[1] == self.vector_dim:
            return vectors
        if vectors.shape[1] > self.vector_dim:
            return vectors[:, : self.vector_dim]
        pad_width = self.vector_dim - vectors.shape[1]
        return np.pad(vectors, ((0, 0), (0, pad_width)), mode="constant")

    def _hash_embedding(self, text: str) -> np.ndarray:
        seed = abs(hash(text)) % (2**32)
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(self.vector_dim)
        norm = np.linalg.norm(vec)
        if norm == 0:
            return vec.astype(np.float32)
        return (vec / norm).astype(np.float32)


class _NumpyFlatIndex:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self._matrix = np.empty((0, dim), dtype=np.float32)

    def add(self, vectors: np.ndarray) -> None:
        if vectors.size == 0:
            return
        self._matrix = np.vstack([self._matrix, vectors])

    def search(self, queries: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        if queries.ndim == 1:
            queries = queries.reshape(1, -1)
        if self._matrix.size == 0:
            distances = np.full((queries.shape[0], k), -1.0, dtype=np.float32)
            indices = np.full((queries.shape[0], k), -1, dtype=np.int64)
            return distances, indices

        sims = queries @ self._matrix.T
        top_k = min(k, self._matrix.shape[0])
        idx = np.argsort(-sims, axis=1)[:, :top_k]
        dist = np.take_along_axis(sims, idx, axis=1)

        if top_k < k:
            pad = k - top_k
            idx = np.pad(idx, ((0, 0), (0, pad)), constant_values=-1)
            dist = np.pad(dist, ((0, 0), (0, pad)), constant_values=-1.0)

        return dist.astype(np.float32), idx.astype(np.int64)


class MemoryStream:
    """FAISS-backed long-term memory stream with local embedding and rule reranking."""

    def __init__(
        self,
        db_path: str | Path,
        embedding_provider: EmbeddingProvider | None = None,
        vector_dim: int = 384,
    ) -> None:
        self.embedding_provider = embedding_provider or LocalSentenceTransformerEmbedder(
            vector_dim=vector_dim
        )
        self.vector_dim = int(self.embedding_provider.vector_dim)

        self._db_path = Path(db_path).resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_table()

        self._index, self._faiss_enabled = self._build_index(self.vector_dim)
        self._row_ids: list[int] = []
        self._record_cache: dict[int, MemoryRecord] = {}
        self._bootstrap_from_db()

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None

    def add_memory(
        self,
        *,
        agent_id: str,
        text: str,
        tick: int,
        channel: str,
        metadata: dict[str, Any] | None = None,
        price_shock: float = 0.0,
        risk_relevance: float = 0.0,
        importance: float | None = None,
    ) -> int:
        payload = str(text).strip()
        if not payload:
            raise ValueError("memory text must be non-empty")

        metadata_obj = dict(metadata or {})
        score = (
            float(importance)
            if importance is not None
            else self.compute_importance(
                text=payload,
                channel=channel,
                current_tick=tick,
                event_tick=tick,
                price_shock=price_shock,
                risk_relevance=risk_relevance,
            )
        )
        embedding = self.embedding_provider.encode([payload]).astype(np.float32)
        if embedding.shape[1] != self.vector_dim:
            raise ValueError("embedding dimension mismatch")

        cur = self._conn.execute(
            """
            INSERT INTO memory_stream (
                agent_id,
                text,
                channel,
                tick,
                importance,
                metadata_json,
                embedding_blob,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(agent_id),
                payload,
                str(channel).upper(),
                int(tick),
                float(score),
                json.dumps(metadata_obj, ensure_ascii=False),
                sqlite3.Binary(embedding.tobytes()),
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        record_id = int(cur.lastrowid)

        self._index.add(embedding)
        self._row_ids.append(record_id)
        self._record_cache[record_id] = MemoryRecord(
            record_id=record_id,
            agent_id=str(agent_id),
            text=payload,
            channel=str(channel).upper(),
            tick=int(tick),
            importance=float(score),
            metadata=metadata_obj,
        )
        return record_id

    def query(
        self,
        *,
        agent_id: str,
        query_text: str,
        top_k: int = 5,
        current_tick: int | None = None,
        price_shock: float = 0.0,
        risk_relevance: float = 0.0,
    ) -> list[dict[str, Any]]:
        if top_k <= 0:
            return []
        if not self._row_ids:
            return []

        qvec = self.embedding_provider.encode([str(query_text)])
        candidate_k = min(max(top_k * 6, top_k), len(self._row_ids))
        distances, indices = self._index.search(qvec.astype(np.float32), candidate_k)

        results: list[dict[str, Any]] = []
        for dist, idx in zip(distances[0], indices[0]):
            if int(idx) < 0:
                continue
            record_id = self._row_ids[int(idx)]
            record = self._record_cache.get(record_id)
            if record is None or record.agent_id != str(agent_id):
                continue

            rerank = self.compute_importance(
                text=record.text,
                channel=record.channel,
                current_tick=current_tick if current_tick is not None else record.tick,
                event_tick=record.tick,
                price_shock=price_shock,
                risk_relevance=risk_relevance,
            )
            score = 0.6 * float(record.importance) + 0.3 * float(dist) + 0.1 * rerank
            results.append(
                {
                    "record_id": record.record_id,
                    "text": record.text,
                    "channel": record.channel,
                    "tick": record.tick,
                    "metadata": record.metadata,
                    "score": score,
                }
            )

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k]

    def compute_importance(
        self,
        *,
        text: str,
        channel: str,
        current_tick: int,
        event_tick: int,
        price_shock: float,
        risk_relevance: float,
    ) -> float:
        channel_norm = str(channel).upper()
        channel_weight = {
            "SYSTEM_NEWS": 1.0,
            "PUBLIC_CHANNEL": 0.9,
            "FORUM": 0.75,
            "PRIVATE_CHANNEL": 0.55,
        }.get(channel_norm, 0.6)

        price_component = min(1.0, abs(float(price_shock)))
        risk_component = min(1.0, max(0.0, float(risk_relevance)))

        age = max(0, int(current_tick) - int(event_tick))
        time_decay = math.exp(-age / 120.0)
        freshness = 1.0 if age <= 5 else 0.4

        novelty = self._novelty_score(text)

        score = (
            0.35 * price_component
            + 0.25 * risk_component
            + 0.20 * channel_weight
            + 0.10 * time_decay
            + 0.10 * novelty
        )
        score += 0.05 * freshness
        return float(max(0.0, min(1.5, score)))

    def _novelty_score(self, text: str) -> float:
        tokens = {token for token in str(text).lower().split() if token}
        if not tokens:
            return 0.0

        if not self._record_cache:
            return 1.0

        recent_records = sorted(
            self._record_cache.values(),
            key=lambda r: r.tick,
            reverse=True,
        )[:15]
        historical_tokens = set()
        for rec in recent_records:
            historical_tokens.update(rec.text.lower().split())

        unique_tokens = tokens - historical_tokens
        return len(unique_tokens) / max(1, len(tokens))

    def _init_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_stream (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                text TEXT NOT NULL,
                channel TEXT NOT NULL,
                tick INTEGER NOT NULL,
                importance REAL NOT NULL,
                metadata_json TEXT,
                embedding_blob BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _build_index(self, dim: int) -> tuple[Any, bool]:
        try:
            import faiss  # type: ignore

            return faiss.IndexFlatIP(dim), True
        except Exception:  # noqa: BLE001
            return _NumpyFlatIndex(dim), False

    def _bootstrap_from_db(self) -> None:
        rows = self._conn.execute(
            """
            SELECT id, agent_id, text, channel, tick, importance, metadata_json, embedding_blob
            FROM memory_stream
            ORDER BY id ASC
            """
        ).fetchall()

        if not rows:
            return

        embeddings: list[np.ndarray] = []
        for row in rows:
            record_id = int(row[0])
            metadata = json.loads(row[6]) if row[6] else {}
            embedding = np.frombuffer(row[7], dtype=np.float32)
            if embedding.size != self.vector_dim:
                # Re-embed corrupted/incompatible rows deterministically.
                embedding = self.embedding_provider.encode([str(row[2])])[0].astype(np.float32)
            embeddings.append(embedding)
            self._row_ids.append(record_id)
            self._record_cache[record_id] = MemoryRecord(
                record_id=record_id,
                agent_id=str(row[1]),
                text=str(row[2]),
                channel=str(row[3]),
                tick=int(row[4]),
                importance=float(row[5]),
                metadata=metadata,
            )

        matrix = np.vstack(embeddings).astype(np.float32)
        self._index.add(matrix)


__all__ = [
    "MemoryStream",
    "MemoryRecord",
    "EmbeddingProvider",
    "LocalSentenceTransformerEmbedder",
]

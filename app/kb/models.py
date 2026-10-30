"""The two local models: an embedder (bi-encoder) and a reranker (cross-encoder).

Both run on the CPU through ONNX Runtime (fastembed), in-process: no API key, no network at
query time, no usage counted against the model subscription. The weights are downloaded
once (``python -m app.kb models``) into ``SRE_KB_MODEL_DIR``; the worker image has them baked
in, and CI caches them.

- ``BAAI/bge-small-en-v1.5``: 384 dimensions, 33M parameters, max 512 wordpieces.
- ``Xenova/ms-marco-MiniLM-L-6-v2``: 22M parameters; scores a (query, passage) pair.

``hash`` and ``overlap`` are deterministic stand-ins with the same interfaces, for unit tests
and for machines that cannot download the weights. They are not semantic: never use them to
judge retrieval quality.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Sequence
from typing import Protocol

DIM = 384
DEFAULT_EMBEDDER = "BAAI/bge-small-en-v1.5"
DEFAULT_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str

    def documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    name: str

    def scores(self, query: str, passages: Sequence[str]) -> list[float]:
        """Relevance in [0, 1] for each passage."""
        ...


class FastEmbedder:
    def __init__(self, name: str = DEFAULT_EMBEDDER, cache_dir: str | None = None, offline: bool = False) -> None:
        from fastembed import TextEmbedding

        self.name = name
        self._model = TextEmbedding(name, cache_dir=cache_dir, local_files_only=offline)

    def documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(list(texts))]

    def query(self, text: str) -> list[float]:
        # bge wants an instruction in front of a query; query_embed adds the model's own.
        return next(iter(self._model.query_embed(text))).tolist()


class FastReranker:
    def __init__(self, name: str = DEFAULT_RERANKER, cache_dir: str | None = None, offline: bool = False) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.name = name
        self._model = TextCrossEncoder(name, cache_dir=cache_dir, local_files_only=offline)

    def scores(self, query: str, passages: Sequence[str]) -> list[float]:
        # The model's logits are unbounded; a sigmoid makes a threshold readable.
        return [_sigmoid(float(s)) for s in self._model.rerank(query, list(passages))]


class HashEmbedder:
    """Words and word pairs hashed into DIM buckets, L2-normalised. Lexical, deterministic."""

    name = "hash"

    def documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        v = [0.0] * DIM
        for feature in [*words, *(f"{a} {b}" for a, b in itertools.pairwise(words))]:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            v[h % DIM] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class OverlapReranker:
    """The share of the query's words found in the passage."""

    name = "overlap"

    def scores(self, query: str, passages: Sequence[str]) -> list[float]:
        q = set(_WORD.findall(query.lower()))
        return [len(q & set(_WORD.findall(p.lower()))) / (len(q) or 1) for p in passages]


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def embedder(name: str, cache_dir: str | None = None, offline: bool = False) -> Embedder:
    return HashEmbedder() if name == "hash" else FastEmbedder(name, cache_dir, offline)


def reranker(name: str, cache_dir: str | None = None, offline: bool = False) -> Reranker:
    return OverlapReranker() if name == "overlap" else FastReranker(name, cache_dir, offline)


def download(embedder_name: str, reranker_name: str, cache_dir: str | None) -> None:
    """Fetch the weights (when building the worker image, and in CI)."""
    FastEmbedder(embedder_name, cache_dir)
    FastReranker(reranker_name, cache_dir)

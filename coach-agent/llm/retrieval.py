"""Knowledge retrieval (BUILD_SPEC §8): chunk knowledge/*.md on `## [chX]`
headers and rank chunks for a query.

v1 uses BM25 keyword ranking in memory: the Anthropic API has no embeddings
endpoint, and the corpus is two short files. Swap `rank` for embeddings later
without changing callers.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config.loader import KNOWLEDGE_DIR

_HEADER = re.compile(r"^## \[(?P<ref>[^\]]+)\]\s*(?P<title>.*)$")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "be", "as",
         "at", "by", "with", "that", "this", "are", "you", "your", "my", "i", "what", "how",
         "do", "does", "should", "can", "when", "why", "if", "from", "than", "then", "not"}


@dataclass(frozen=True)
class Chunk:
    source: str  # file stem
    ref: str  # e.g. "ch2"
    title: str
    text: str

    def render(self) -> str:
        return f"[{self.source} {self.ref}] {self.title}\n{self.text.strip()}"


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def chunk_file(path: Path) -> list[Chunk]:
    out: list[Chunk] = []
    cur: tuple[str, str] | None = None
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEADER.match(line)
        if m or line.startswith("## "):
            if cur:
                out.append(Chunk(path.stem, cur[0], cur[1], "\n".join(buf)))
            cur = (m.group("ref"), m.group("title")) if m else None  # untagged sections are skipped
            buf = []
        elif cur:
            buf.append(line)
    if cur:
        out.append(Chunk(path.stem, cur[0], cur[1], "\n".join(buf)))
    return out


@lru_cache(maxsize=1)
def corpus(knowledge_dir: Path = KNOWLEDGE_DIR) -> tuple[Chunk, ...]:
    return tuple(c for p in sorted(knowledge_dir.glob("*.md")) for c in chunk_file(p))


def rank(query: str, chunks: tuple[Chunk, ...], k: int, k1: float = 1.5, b: float = 0.75) -> list[Chunk]:
    """BM25 (k1, b are the standard textbook defaults, not coaching numbers)."""
    q = tokens(query)
    if not q or not chunks:
        return []
    docs = [tokens(c.title + " " + c.text) for c in chunks]
    avg = sum(len(d) for d in docs) / len(docs)
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    scores = []
    for i, d in enumerate(docs):
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        scores.append((s, i))
    ranked = sorted((x for x in scores if x[0] > 0), key=lambda x: (-x[0], x[1]))
    return [chunks[i] for _, i in ranked[:k]]


def retrieve(query: str, k: int) -> list[Chunk]:
    return rank(query, corpus(), k)

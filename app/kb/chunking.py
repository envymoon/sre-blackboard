"""Documents and chunks: front matter, heading-based chunking, stable section labels.

A document is Markdown with a YAML front matter block (id, kind, title, services, version).
It is split on its ``##``/``###`` headings. A section over ``max`` tokens is split further at
paragraph, list item or code fence boundaries, with ``overlap`` tokens of the previous piece
carried over; a section under ``min`` is merged into the chunk before it. Code blocks are
never cut in half.

Two parameter sets (``PRESETS``): ``original`` is the original design's (target 450, min 200,
max 700, overlap 80), made for long runbooks; on this corpus, whose documents are about 300
tokens, it makes one chunk per document. ``section`` keeps every heading's section apart
(min 0). The retrieval evaluation compares the two (eval/retrieval).

Every chunk keeps the slugs of the sections it holds (``RB-PAYMENT#first-checks``). The
retrieval evaluation labels sections, not chunks, so its labels survive a change of the
chunking parameters.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

KINDS = ("runbook", "postmortem")
Kind = Literal["runbook", "postmortem"]

_TOKEN = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16})")


def tokens(text: str) -> int:
    """A model-independent size: words and punctuation. A wordpiece tokenizer gives ~1.3x."""
    return len(_TOKEN.findall(text))


def slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def redact(text: str) -> str:
    """Nothing that looks like a key or a token goes into the index (or into a prompt)."""
    return _SECRET.sub("[REDACTED]", text)


@dataclass(frozen=True)
class ChunkParams:
    name: str
    target: int
    min: int
    max: int
    overlap: int


PRESETS = {
    p.name: p
    for p in (
        ChunkParams("original", target=450, min=200, max=700, overlap=80),
        ChunkParams("section", target=450, min=0, max=700, overlap=80),
    )
}
DEFAULT_CHUNKING = "section"


@dataclass(frozen=True)
class Document:
    id: str
    kind: Kind
    title: str
    services: tuple[str, ...]
    version: str
    body: str
    path: str = ""

    @property
    def content_hash(self) -> str:
        meta = f"{self.kind}\n{self.title}\n{','.join(self.services)}\n{self.version}\n"
        return hashlib.sha256((meta + self.body).encode()).hexdigest()


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    ordinal: int
    headings: tuple[str, ...]  # the sections this chunk holds, in order
    content: str
    sections: tuple[str, ...] = field(default=())  # "<doc id>#<heading slug>" for each heading

    @property
    def chunk_id(self) -> str:
        return f"{self.doc_id}#{self.ordinal:02d}"

    @property
    def tokens(self) -> int:
        return tokens(self.content)

    def text_for_embedding(self, title: str) -> str:
        """The chunk with its document title and headings in front: a section called
        "First checks" means little on its own."""
        return f"{title} > {' / '.join(self.headings)}\n{self.content}"


def parse(text: str, path: str = "") -> Document:
    if not text.startswith("---\n"):
        raise ValueError(f"{path or 'document'}: missing front matter (--- ... ---)")
    raw_meta, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError(f"{path or 'document'}: front matter is not closed")
    meta = yaml.safe_load(raw_meta) or {}
    missing = [k for k in ("id", "kind", "title", "services", "version") if not meta.get(k)]
    if missing:
        raise ValueError(f"{path or 'document'}: front matter lacks {', '.join(missing)}")
    if meta["kind"] not in KINDS:
        raise ValueError(f"{path or 'document'}: kind must be one of {', '.join(KINDS)}")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]+", str(meta["id"])):
        raise ValueError(f"{path or 'document'}: id must be upper case letters, digits and dashes")
    return Document(
        id=str(meta["id"]),
        kind=meta["kind"],
        title=str(meta["title"]),
        services=tuple(str(s) for s in meta["services"]),
        version=str(meta["version"]),
        body=body.strip(),
        path=path,
    )


def load_dir(directory: Path) -> list[Document]:
    docs = [parse(p.read_text(), str(p)) for p in sorted(directory.rglob("*.md")) if p.name != "README.md"]
    seen: dict[str, str] = {}
    for d in docs:
        if d.id in seen:
            raise ValueError(f"{d.path}: id {d.id} is also used by {seen[d.id]}")
        seen[d.id] = d.path
    return docs


def _sections(body: str) -> list[tuple[str, str]]:
    """(heading, text) pairs; text before the first heading is the "overview" section."""
    marks = list(_HEADING.finditer(body))
    out = []
    intro = body[: marks[0].start()] if marks else body
    if intro.strip():
        out.append(("Overview", intro.strip()))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        text = body[m.start() : end].strip()
        out.append((m.group(2), text))
    return out


def _blocks(text: str) -> list[str]:
    """Paragraphs, list items and fenced code blocks: the places a section may be cut."""
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        starts_item = not in_fence and re.match(r"\s{0,1}([-*]|\d+\.)\s", line) is not None
        if not in_fence and (not line.strip() or starts_item) and buf:
            blocks.append("\n".join(buf))
            buf = []
        if line.strip() or in_fence:
            buf.append(line)
    if buf:
        blocks.append("\n".join(buf))
    return blocks


def _tail(text: str, n: int) -> str:
    """The last ``n`` tokens of ``text``, starting at a word boundary. Fence lines are left
    out: carried over alone, a closing ``` would open a code block in the next piece."""
    words = "\n".join(line for line in text.splitlines() if not line.startswith("```")).split()
    out: list[str] = []
    for w in reversed(words):
        out.insert(0, w)
        if tokens(" ".join(out)) >= n:
            break
    return " ".join(out)


def _split(text: str, params: ChunkParams) -> list[str]:
    if tokens(text) <= params.max:
        return [text]
    pieces: list[str] = []
    buf = ""
    for block in _blocks(text):
        if buf and tokens(buf) + tokens(block) > params.target:
            pieces.append(buf)
            buf = f"{_tail(buf, params.overlap)}\n\n{block}"
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        pieces.append(buf)
    return pieces


def chunk(doc: Document, params: ChunkParams = PRESETS[DEFAULT_CHUNKING]) -> list[Chunk]:
    merged: list[tuple[list[str], str]] = []  # (headings, text)
    for heading, text in _sections(doc.body):
        for i, piece in enumerate(_split(text, params)):
            # Only the first piece of a section may be merged into the chunk before it.
            small = tokens(piece) < params.min
            if i == 0 and merged and small and tokens(merged[-1][1]) + tokens(piece) <= params.max:
                headings, prev = merged[-1]
                if heading not in headings:
                    headings.append(heading)
                merged[-1] = (headings, f"{prev}\n\n{piece}")
            else:
                merged.append(([heading], piece))
    return [
        Chunk(
            doc_id=doc.id,
            ordinal=n,
            headings=tuple(headings),
            content=redact(text),
            sections=tuple(f"{doc.id}#{slug(h)}" for h in headings),
        )
        for n, (headings, text) in enumerate(merged, 1)
    ]

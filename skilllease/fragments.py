"""Deterministic segmentation of a SKILL.md into checkable fragments.

A fragment is one Markdown block: a paragraph, a list item, or a fenced code
block. Headings, blank lines, and front matter are not fragments. Ids are
assigned in file order and are stable as long as the block order is stable.
The anchor hash lets the runner notice when a bound fragment's text changed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from pathlib import Path

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^\s*#{1,6}\s")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


@dataclass(frozen=True)
class Fragment:
    id: str
    start_line: int
    end_line: int
    kind: str
    text: str

    @property
    def sha256(self) -> str:
        return text_hash(self.text)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sha256"] = self.sha256
        return d


def text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _strip_front_matter(lines: list[str]) -> int:
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def segment(text: str) -> list[Fragment]:
    lines = text.splitlines()
    start = _strip_front_matter(lines)
    out: list[Fragment] = []
    i = start
    n = len(lines)

    def emit(s: int, e: int, kind: str) -> None:
        body = "\n".join(lines[s:e])
        if body.strip():
            out.append(Fragment(f"f{len(out) + 1:02d}", s + 1, e, kind, body))

    while i < n:
        line = lines[i]
        if not line.strip() or HEADING.match(line):
            i += 1
            continue
        if FENCE.match(line):
            fence = FENCE.match(line).group(1)
            j = i + 1
            while j < n and not lines[j].strip().startswith(fence):
                j += 1
            emit(i, min(j + 1, n), "code")
            i = j + 1
            continue
        if LIST_ITEM.match(line):
            j = i + 1
            while j < n and lines[j].strip() and not LIST_ITEM.match(lines[j]) and not FENCE.match(lines[j]) and not HEADING.match(lines[j]):
                j += 1
            emit(i, j, "item")
            i = j
            continue
        j = i + 1
        while j < n and lines[j].strip() and not FENCE.match(lines[j]) and not HEADING.match(lines[j]) and not LIST_ITEM.match(lines[j]):
            j += 1
        emit(i, j, "paragraph")
        i = j
    return out


def segment_file(path: str | Path) -> list[Fragment]:
    return segment(Path(path).read_text(encoding="utf-8"))

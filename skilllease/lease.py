"""A lease is a repository plus a resolved skills directory plus one interpreter."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .model import Report, summarize
from .runner import run

DEFAULT_SKILLS_DIR = ".agents/skills"
POLICY_NAME = "skilllease.json"


@dataclass(frozen=True)
class Lease:
    root: Path
    skills_dir: Path
    python: Path

    def __post_init__(self) -> None:
        if not self.skills_dir.is_dir():
            raise ValueError(f"skills directory missing: {self.skills_dir}")


@dataclass(frozen=True)
class SkillEntry:
    id: str
    skill_path: Path
    sidecar_path: Path


@dataclass
class SkillReport:
    id: str
    report: Report

    def to_dict(self) -> dict:
        return {"id": self.id, "report": self.report.to_dict()}


@dataclass
class LeaseReport:
    root: str
    skills_dir: str
    python: str
    checked_at: str
    skills: list[SkillReport]
    summary: dict[str, int]
    elapsed_ms: int

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "skills_dir": self.skills_dir,
            "python": self.python,
            "checked_at": self.checked_at,
            "skills": [s.to_dict() for s in self.skills],
            "summary": self.summary,
            "elapsed_ms": self.elapsed_ms,
        }


def find_root(start: Path) -> Path:
    cur = start.resolve()
    for path in (cur, *cur.parents):
        if (path / ".git").exists() or (path / POLICY_NAME).is_file():
            return path
    return cur


def read_policy(root: Path) -> dict:
    path = root / POLICY_NAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{POLICY_NAME} must be an object")
    return data


def _resolve_under(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path)


def resolve_python(root: Path, override: str | None, policy: dict) -> Path:
    if override:
        return Path(override).expanduser()
    named = policy.get("python")
    if named:
        return _resolve_under(root, named)
    venv = root / ".venv" / "bin" / "python"
    if venv.is_file():
        return venv
    return Path(sys.executable)


def open_lease(
    root: Path | None = None,
    skills_dir: Path | str | None = None,
    python: str | None = None,
    cwd: Path | None = None,
) -> Lease:
    here = (cwd or Path.cwd()).resolve()
    resolved_root = root.resolve() if root is not None else find_root(here)
    policy = read_policy(resolved_root)
    if skills_dir is not None:
        directory = _resolve_under(resolved_root, skills_dir)
    else:
        named = policy.get("skillsDir") or policy.get("skills_dir")
        directory = _resolve_under(resolved_root, named) if named else resolved_root / DEFAULT_SKILLS_DIR
    interpreter = resolve_python(resolved_root, python, policy)
    return Lease(root=resolved_root, skills_dir=directory.resolve(), python=interpreter)


def discover_skills(lease: Lease) -> list[SkillEntry]:
    found: list[SkillEntry] = []
    for skill_md in sorted(lease.skills_dir.rglob("SKILL.md")):
        if skill_md.name != "SKILL.md":
            continue
        parent = skill_md.parent
        rel = parent.relative_to(lease.skills_dir)
        skill_id = "." if rel == Path(".") else rel.as_posix()
        found.append(SkillEntry(id=skill_id, skill_path=skill_md, sidecar_path=parent / "SKILL.lease.json"))
    return found


def _add_summaries(into: dict[str, int], part: dict[str, int]) -> None:
    for key, value in part.items():
        into[key] = into.get(key, 0) + value


def check_lease(lease: Lease) -> LeaseReport:
    t0 = time.monotonic()
    skills: list[SkillReport] = []
    summary = summarize([])
    for entry in discover_skills(lease):
        sidecar = entry.sidecar_path if entry.sidecar_path.is_file() else None
        report = run(entry.skill_path, sidecar, str(lease.python), cwd=lease.root)
        skills.append(SkillReport(id=entry.id, report=report))
        _add_summaries(summary, report.summary)
    summary["skills"] = len(skills)
    return LeaseReport(
        root=str(lease.root),
        skills_dir=str(lease.skills_dir),
        python=str(lease.python),
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        skills=skills,
        summary=summary,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

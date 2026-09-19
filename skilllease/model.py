"""The sidecar and the report. Every other module reads and writes these shapes.

Sidecar: `SKILL.lease.json` beside `SKILL.md`. It binds fragments (by id and
text hash) to checks. A fragment listed with no checks was looked at by the
binder and declined; a fragment absent from the sidecar is unchecked.

Report: the runner's output. One record per fragment in the skill, in file
order, each with its status and its checks' observed values.

Fragment status precedence: violated > unknown > changed > satisfied.
A fragment with no checks is unchecked. A fragment whose text no longer
matches its anchor hash is unknown with reason "anchor changed".
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal, Union

Status = Literal["satisfied", "changed", "violated", "unknown", "unchecked"]
PRECEDENCE: tuple[Status, ...] = ("violated", "unknown", "changed", "satisfied")


@dataclass(frozen=True)
class PackageCheck:
    """Installed distribution version against a constraint.

    satisfied: observed == bound. changed: observed != bound but satisfies
    constraint. violated: observed fails constraint. unknown: not installed.
    """

    dist: str
    bound: str
    constraint: str
    type: Literal["package"] = "package"


@dataclass(frozen=True)
class SymbolCheck:
    """A Python callable's presence and signature.

    params are the parameter names the instruction relies on.
    satisfied: calling structure identical, ignoring annotations. changed: structure differs but every
    listed param is present. violated: symbol missing or a listed param
    missing. unknown: module import failed for a reason other than absence.
    """

    module: str
    qualname: str
    params: list[str]
    signature: str
    type: Literal["symbol"] = "symbol"


@dataclass(frozen=True)
class PredicateCheck:
    """A boolean Python expression evaluated in the target interpreter.

    satisfied: value == expect. violated: value differs or execution raises.
    unknown: invalid expression, interrupted or unavailable probe.
    """

    expr: str
    expect: bool
    note: str = ""
    type: Literal["predicate"] = "predicate"


Check = Union[PackageCheck, SymbolCheck, PredicateCheck]


@dataclass
class Binding:
    fragment: str
    sha256: str
    checks: list[Check] = field(default_factory=list)
    reason: str = ""


@dataclass
class Sidecar:
    skill: str
    python: str
    bindings: list[Binding]
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    check: dict
    status: Status
    observed: str
    evidence: str


@dataclass
class FragmentResult:
    id: str
    start_line: int
    end_line: int
    kind: str
    text: str
    status: Status
    reason: str
    checks: list[CheckResult]


@dataclass
class Report:
    skill: str
    sidecar: str
    python: str
    checked_at: str
    fragments: list[FragmentResult]
    summary: dict[str, int]
    elapsed_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


def combine(statuses: list[Status]) -> Status:
    if not statuses:
        return "unchecked"
    for s in PRECEDENCE:
        if s in statuses:
            return s
    return "satisfied"


def summarize(fragments: list[FragmentResult]) -> dict[str, int]:
    keys: list[Status] = ["satisfied", "changed", "violated", "unknown", "unchecked"]
    out = {k: 0 for k in keys}
    for f in fragments:
        out[f.status] += 1
    out["total"] = len(fragments)
    return out


def check_from_dict(d: dict) -> Check:
    t = d.get("type")
    body = {k: v for k, v in d.items() if k != "type"}
    if t == "package":
        return PackageCheck(**body)
    if t == "symbol":
        return SymbolCheck(**body)
    if t == "predicate":
        return PredicateCheck(**body)
    raise ValueError(f"unknown check type: {t!r}")


def sidecar_from_dict(d: dict) -> Sidecar:
    bindings = [
        Binding(
            fragment=b["fragment"],
            sha256=b["sha256"],
            checks=[check_from_dict(c) for c in b.get("checks", [])],
            reason=b.get("reason", ""),
        )
        for b in d.get("bindings", [])
    ]
    return Sidecar(skill=d["skill"], python=d.get("python", "python3"), bindings=bindings, version=d.get("version", 1))

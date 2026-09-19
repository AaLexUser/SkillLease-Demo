"""Run a sidecar's checks against a skill and produce a Report."""

from __future__ import annotations

import json
import ast
import io
import re
import subprocess
import sys
import time
import tokenize
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion

from . import fragments as frag
from .model import (
    Check,
    CheckResult,
    FragmentResult,
    PackageCheck,
    PredicateCheck,
    Report,
    Sidecar,
    Status,
    SymbolCheck,
    combine,
    sidecar_from_dict,
    summarize,
)

PROBE = Path(__file__).with_name("probe.py")


def run(skill_path: str | Path, sidecar_path: str | Path | None = None, python: str | None = None, timeout_s: float = 20.0, cwd: str | Path | None = None) -> Report:
    t0 = time.monotonic()
    skill = Path(skill_path)
    sidecar_file = Path(sidecar_path) if sidecar_path else skill.with_suffix(".lease.json")
    sidecar = load_sidecar(sidecar_file)
    interpreter = python or resolve_python(sidecar.python, sidecar_file)
    by_id = {b.fragment: b for b in sidecar.bindings}

    pending: list[tuple[int, Check]] = []
    results: list[FragmentResult] = []
    for fragment in frag.segment_file(skill):
        binding = by_id.get(fragment.id)
        result = FragmentResult(status="unchecked", reason="", checks=[], **asdict(fragment))
        if binding is None:
            pass
        elif binding.sha256 != fragment.sha256:
            result.status, result.reason = "unknown", "anchor changed"
        elif not binding.checks:
            result.reason = binding.reason
        else:
            result.reason = binding.reason
            for check in binding.checks:
                pending.append((len(results), check))
        results.append(result)

    observations = probe([c for _, c in pending], interpreter, timeout_s, cwd=cwd)
    for (index, check), observation in zip(pending, observations):
        results[index].checks.append(evaluate(check, observation))
    for result in results:
        if result.checks:
            result.status = combine([c.status for c in result.checks])

    return Report(
        skill=str(skill),
        sidecar=str(sidecar_file),
        python=interpreter,
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        fragments=results,
        summary=summarize(results),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def load_sidecar(path: Path) -> Sidecar:
    if not path.exists():
        return Sidecar(skill="", python=sys.executable, bindings=[])
    return sidecar_from_dict(json.loads(path.read_text(encoding="utf-8")))


def resolve_python(python: str, sidecar_file: Path) -> str:
    p = Path(python)
    if p.is_absolute() or len(p.parts) == 1:
        return python
    return str((sidecar_file.parent / p).resolve())


def probe(checks: list[Check], interpreter: str, timeout_s: float, cwd: str | Path | None = None) -> list[dict]:
    if not checks:
        return []
    cmd = [interpreter, str(PROBE), "--timeout", str(timeout_s)]
    try:
        proc = subprocess.run(cmd, input=json.dumps([asdict(c) for c in checks]), capture_output=True, text=True, cwd=cwd)
    except OSError as e:
        return [{"error": f"cannot start {interpreter}: {e}"}] * len(checks)
    if proc.returncode != 0:
        return [{"error": f"probe exited {proc.returncode}: {proc.stderr.strip()[-300:]}"}] * len(checks)
    try:
        observations = json.loads(proc.stdout)
    except ValueError:
        return [{"error": "probe returned invalid JSON"}] * len(checks)
    if not isinstance(observations, list) or any(not isinstance(o, dict) for o in observations):
        return [{"error": "probe returned invalid observations"}] * len(checks)
    if len(observations) != len(checks):
        return [{"error": f"probe returned {len(observations)} observations for {len(checks)} checks"}] * len(checks)
    return observations


def evaluate(check: Check, obs: dict) -> CheckResult:
    if obs.get("error"):
        return result(check, "unknown", "", f"{describe(check)}: {obs['error']}")
    if isinstance(check, PackageCheck):
        return evaluate_package(check, obs)
    if isinstance(check, SymbolCheck):
        return evaluate_symbol(check, obs)
    return evaluate_predicate(check, obs)


def evaluate_package(check: PackageCheck, obs: dict) -> CheckResult:
    version = obs.get("version")
    if version is None:
        return result(check, "unknown", "", f"{check.dist} is not installed")
    if version == check.bound:
        return result(check, "satisfied", version, f"{check.dist} {version} == bound {check.bound} ({check.constraint})")
    try:
        ok = SpecifierSet(check.constraint).contains(version, prereleases=True)
    except (InvalidSpecifier, InvalidVersion) as e:
        return result(check, "unknown", version, f"{check.dist} {version}: cannot evaluate {check.constraint!r}: {e}")
    if ok:
        return result(check, "changed", version, f"{check.dist} {version} differs from bound {check.bound}, satisfies {check.constraint}")
    return result(check, "violated", version, f"{check.dist} {version} fails {check.constraint}")


def evaluate_symbol(check: SymbolCheck, obs: dict) -> CheckResult:
    name = f"{check.module}.{check.qualname}"
    if not obs.get("exists"):
        return result(check, "violated", "", obs.get("absent") or f"{name} is missing")
    signature = obs.get("signature")
    if signature is None:
        return result(check, "unknown", "", f"{name} exists but its signature is not introspectable")
    missing = [p for p in check.params if p not in obs.get("params", [])]
    if missing:
        names = ", ".join(f"'{p}'" for p in missing)
        return result(check, "violated", signature, f"{check.qualname} has no parameter {names} (signature: {signature})")
    if signature == check.signature:
        return result(check, "satisfied", signature, f"{check.qualname} signature unchanged: {signature}")
    if stable_object_defaults(signature) == stable_object_defaults(check.signature):
        return result(check, "satisfied", signature, f"{check.qualname} signature unchanged apart from object identity: {signature}")
    expected_structure = signature_structure(check.signature)
    if expected_structure is not None and expected_structure == signature_structure(signature):
        return result(check, "satisfied", signature, f"{check.qualname} calling structure unchanged despite annotation rendering: {signature}")
    present = ", ".join(check.params) or "none listed"
    return result(check, "changed", signature, f"{check.qualname} signature changed from {check.signature} to {signature}; relied-on params present: {present}")


def stable_object_defaults(signature: str) -> str:
    lines = signature.splitlines(keepends=True)
    offsets = [sum(map(len, lines[:n])) for n in range(len(lines))]
    try:
        strings = [(offsets[t.start[0] - 1] + t.start[1], offsets[t.end[0] - 1] + t.end[1])
                   for t in tokenize.generate_tokens(io.StringIO(signature).readline)
                   if t.type == tokenize.STRING]
    except (tokenize.TokenError, IndentationError):
        return signature
    return re.sub(r"<object object at 0x[0-9a-fA-F]+>",
                  lambda m: m.group() if any(a <= m.start() < b for a, b in strings) else "<object object>",
                  signature)


def signature_structure(signature: str) -> str | None:
    try:
        arguments = ast.parse("def probe" + signature + ":\n pass").body[0].args
    except (SyntaxError, ValueError, AttributeError):
        return None
    for arg in arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [arguments.vararg, arguments.kwarg]:
        if arg is not None:
            arg.annotation = None
    return ast.dump(arguments, include_attributes=False)


def evaluate_predicate(check: PredicateCheck, obs: dict) -> CheckResult:
    if raised := obs.get("raised"):
        observed = f"{raised['type']}: {raised['message']}"
        return result(check, "violated", observed, f"{check.expr} raised {observed}")
    value = obs.get("value")
    observed = json.dumps(value)
    if type(value) is not bool:
        return result(check, "unknown", observed, "predicate did not return a boolean")
    if value is check.expect:
        return result(check, "satisfied", observed, f"{check.expr} -> {observed}")
    return result(check, "violated", observed, f"{check.expr} -> {observed}, expected {json.dumps(check.expect)}")


def describe(check: Check) -> str:
    if isinstance(check, PackageCheck):
        return check.dist
    if isinstance(check, SymbolCheck):
        return f"{check.module}.{check.qualname}"
    return check.expr


def result(check: Check, status: Status, observed: str, evidence: str) -> CheckResult:
    return CheckResult(check=asdict(check), status=status, observed=observed, evidence=evidence)

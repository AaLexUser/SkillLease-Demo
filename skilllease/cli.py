"""Command line: fragments, check, ci."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import fragments as frag
from .lease import LeaseReport, check_lease, open_lease
from .model import Report
from .runner import run


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="skilllease")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fragments", help="print the skill's fragments as JSON")
    p.add_argument("skill")

    for name in ("check", "ci"):
        p = sub.add_parser(
            name,
            help="run sidecar checks on a skill or every skill in a lease" if name == "check" else "check --strict with GitHub Actions annotations",
        )
        p.add_argument("skill", nargs="?")
        p.add_argument("--root")
        p.add_argument("--skills-dir")
        p.add_argument("--sidecar")
        p.add_argument("--python")
        p.add_argument("--json", dest="json_out")
        if name == "check":
            p.add_argument("--strict", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "fragments":
        print(json.dumps([f.to_dict() for f in frag.segment_file(args.skill)], indent=2))
        return 0

    strict = args.command == "ci" or getattr(args, "strict", False)
    if args.skill and args.skills_dir:
        raise SystemExit("pass a SKILL.md path or --skills-dir, not both")
    if args.skill:
        report: Report | LeaseReport = run(args.skill, args.sidecar, args.python)
        print(table(report))
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        if args.command == "ci":
            for line in annotations(report):
                print(line)
        return exit_code(report, strict=strict)

    lease = open_lease(
        root=Path(args.root) if args.root else None,
        skills_dir=args.skills_dir,
        python=args.python,
    )
    lease_report = check_lease(lease)
    print(lease_table(lease_report))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(lease_report.to_dict(), indent=2), encoding="utf-8")
    if args.command == "ci":
        for line in lease_annotations(lease_report):
            print(line)
    return exit_code(lease_report, strict=strict)


def table(report: Report) -> str:
    lines = []
    for f in report.fragments:
        span = f"{f.start_line}-{f.end_line}"
        lines.append(f"{f.id:<5}{span:<10}{f.status:<11}{f.reason}".rstrip())
        for c in f.checks:
            lines.append(f"     {c.status:<11}{c.evidence}")
    s = report.summary
    lines.append(
        f"summary: {s['satisfied']} satisfied, {s['changed']} changed, {s['violated']} violated, "
        f"{s['unknown']} unknown, {s['unchecked']} unchecked, {s['total']} total ({report.elapsed_ms} ms, {report.python})"
    )
    return "\n".join(lines)


def lease_table(report: LeaseReport) -> str:
    lines = []
    for skill in report.skills:
        lines.append(f"{skill.id}  {skill.report.skill}")
        lines.append(table(skill.report))
    s = report.summary
    lines.append(
        f"lease: {s.get('skills', 0)} skills, {s['satisfied']} satisfied, {s['changed']} changed, "
        f"{s['violated']} violated, {s['unknown']} unknown, {s['unchecked']} unchecked, "
        f"{s['total']} total ({report.elapsed_ms} ms, {report.python})"
    )
    return "\n".join(lines)


def annotations(report: Report) -> list[str]:
    out = []
    for f in report.fragments:
        if f.status != "violated":
            continue
        evidence = "; ".join(c.evidence for c in f.checks if c.status == "violated")
        out.append(f"::error file={report.skill},line={f.start_line},endLine={f.end_line}::{f.id} violated: {evidence}")
    return out


def lease_annotations(report: LeaseReport) -> list[str]:
    out = []
    for skill in report.skills:
        out.extend(annotations(skill.report))
    return out


def exit_code(report: Report | LeaseReport, strict: bool) -> int:
    if report.summary["violated"]:
        return 2
    if strict and report.summary["unknown"]:
        return 3
    return 0

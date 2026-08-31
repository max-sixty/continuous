# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Report token use and spend across recent Tend workflow runs."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import github_cli

RUN_LIMIT = 1000
TOP_SUBJECTS = 20
COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "turns",
)


def _json_documents(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        value, position = decoder.raw_decode(text, position)
        if not isinstance(value, dict):
            raise TypeError(f"{path} contains a non-object JSON value")
        documents.append(value)
    return documents


def _sum(rows: list[dict[str, Any]], field: str) -> int | float:
    return sum(row.get(field) or 0 for row in rows)


def _pick(rows: list[dict[str, Any]], field: str) -> Any:
    return next((row[field] for row in rows if row.get(field) is not None), None)


def build_report(jobs: list[dict[str, Any]], *, skipped: int) -> dict[str, Any]:
    """Collapse job records into run records and report totals."""
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        grouped[int(job["run_id"])].append(job)

    runs: list[dict[str, Any]] = []
    for run_id, rows in grouped.items():
        run = {
            "run_id": run_id,
            "workflow": rows[0]["workflow"],
            "created_at": rows[0]["created_at"],
            "repo": _pick(rows, "repo"),
            "event": _pick(rows, "event"),
            "number": _pick(rows, "number"),
            "head_sha": _pick(rows, "head_sha"),
            **{field: _sum(rows, field) for field in COUNT_FIELDS},
            "cost_usd": _sum(rows, "cost_usd"),
            "partial": any(bool(row.get("partial")) for row in rows),
        }
        run["subject"] = (
            f"#{run['number']}" if run["number"] else str(run["head_sha"] or "?")
        )
        runs.append(run)
    runs.sort(key=lambda run: run["created_at"], reverse=True)

    totals = {
        **{field: _sum(runs, field) for field in COUNT_FIELDS},
        "cost_usd": round(float(_sum(runs, "cost_usd")) + 1e-12, 2),
        "partial_runs": sum(bool(run["partial"]) for run in runs),
        "skipped_runs": skipped,
    }
    return {"runs": runs, "totals": totals}


def _fmt_count(value: float) -> str:
    if value >= 1_000_000:
        return f"{math.floor(value / 100_000) / 10:g}M"
    if value >= 1_000:
        return f"{math.floor(value / 100) / 10:g}K"
    return f"{value:g}"


def _usd(value: float) -> str:
    return f"${round(float(value) + 1e-12, 2):.2f}"


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    ]


def _rollup(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(runs),
        "cost": _sum(runs, "cost_usd"),
        "partial": any(bool(run["partial"]) for run in runs),
        "i": _sum(runs, "input_tokens"),
        "o": _sum(runs, "output_tokens"),
        "cc": _sum(runs, "cache_creation_input_tokens"),
        "cr": _sum(runs, "cache_read_input_tokens"),
    }


def _group_rollups(runs: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[str(run[key])].append(run)
    values = []
    for name, grouped in groups.items():
        values.append(
            {
                "key": name,
                "workflows": ",".join(sorted({run["workflow"] for run in grouped})),
                **_rollup(grouped),
            }
        )
    return values


def render_summary(report: dict[str, Any], *, since: str) -> str:
    """Render the human-readable stderr summary."""
    runs = report["runs"]
    totals = report["totals"]
    floor = "+" if totals["partial_runs"] else ""
    blocks: list[list[str]] = [
        [
            f"{len(runs)} runs since {since}",
            f"Total cost: {_usd(totals['cost_usd'])}{floor}"
            + (
                f" ({totals['partial_runs']} of {len(runs)} runs cost-unknown)"
                if totals["partial_runs"]
                else ""
            ),
            (
                "Tokens: "
                f"{_fmt_count(totals['input_tokens'])} in, "
                f"{_fmt_count(totals['output_tokens'])} out, "
                f"{_fmt_count(totals['cache_creation_input_tokens'])} cache-create, "
                f"{_fmt_count(totals['cache_read_input_tokens'])} cache-read"
            ),
        ]
    ]

    workflows = sorted(
        _group_rollups(runs, "workflow"), key=lambda item: item["cost"], reverse=True
    )
    blocks.append(
        _table(
            [
                [
                    "WORKFLOW",
                    "RUNS",
                    "COST",
                    "INPUT",
                    "OUTPUT",
                    "CACHE-CREATE",
                    "CACHE-READ",
                ]
            ]
            + [
                [
                    item["key"],
                    str(item["n"]),
                    _usd(item["cost"]) + ("+" if item["partial"] else ""),
                    _fmt_count(item["i"]),
                    _fmt_count(item["o"]),
                    _fmt_count(item["cc"]),
                    _fmt_count(item["cr"]),
                ]
                for item in workflows
            ]
        )
    )

    subjects = sorted(
        _group_rollups(runs, "subject"), key=lambda item: item["cost"], reverse=True
    )
    blocks.append(
        _table(
            [["SUBJECT", "RUNS", "COST", "WORKFLOWS", "CACHE-READ"]]
            + [
                [
                    item["key"][:12],
                    str(item["n"]),
                    _usd(item["cost"]) + ("+" if item["partial"] else ""),
                    item["workflows"],
                    _fmt_count(item["cr"]),
                ]
                for item in subjects[:TOP_SUBJECTS]
            ]
        )
    )

    if totals["partial_runs"]:
        unknown = sorted(
            _group_rollups([run for run in runs if run["partial"]], "subject"),
            key=lambda item: item["cr"],
            reverse=True,
        )
        blocks.append(
            _table(
                [["COST-UNKNOWN", "RUNS", "CACHE-READ", "OUTPUT", "WORKFLOWS"]]
                + [
                    [
                        item["key"][:12],
                        str(item["n"]),
                        _fmt_count(item["cr"]),
                        _fmt_count(item["o"]),
                        item["workflows"],
                    ]
                    for item in unknown
                ]
            )
        )

    blocks.append(
        _table(
            [
                [
                    "RUN",
                    "WORKFLOW",
                    "SUBJECT",
                    "COST",
                    "INPUT",
                    "OUTPUT",
                    "CACHE-CREATE",
                    "CACHE-READ",
                    "TIME",
                ]
            ]
            + [
                [
                    str(run["run_id"]),
                    run["workflow"],
                    run["subject"][:12],
                    _usd(run["cost_usd"]) + ("+" if run["partial"] else ""),
                    _fmt_count(run["input_tokens"]),
                    _fmt_count(run["output_tokens"]),
                    _fmt_count(run["cache_creation_input_tokens"]),
                    _fmt_count(run["cache_read_input_tokens"]),
                    run["created_at"][:16],
                ]
                for run in runs
            ]
        )
    )

    notes: list[str] = []
    if len(subjects) > TOP_SUBJECTS:
        notes.append(
            f"Subjects: showing the {TOP_SUBJECTS} costliest of {len(subjects)}; "
            "the JSON on stdout has them all."
        )
    if totals["partial_runs"]:
        notes.append(
            "COST-UNKNOWN lists the runs that emitted no result event, typically "
            "cancelled: their tokens are counted everywhere, their cost is not "
            "recoverable. A '+' marks a cost that is a floor rather than the spend."
        )
    if totals["skipped_runs"]:
        notes.append(
            f"{totals['skipped_runs']} run(s) uploaded no readable "
            "claude-session-logs artifact and are absent entirely: codex-harness "
            "runs, runs that ended before the upload, and torn uploads."
        )
    notes.append(
        "Cost at API list prices — a large multiple of the effective rate on "
        "Claude Code subscriptions."
    )
    blocks.append(notes)
    return "\n\n" + "\n\n".join("\n".join(block) for block in blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        hours = int(args[0]) if args else 168
    except ValueError:
        print(f"usage: {sys.argv[0]} [HOURS] [PREFIX ...]", file=sys.stderr)
        return 2
    extra_prefixes = args[1:] if args else []
    since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo_args = (
        ["-R", os.environ["TARGET_REPO"]] if os.environ.get("TARGET_REPO") else []
    )

    workflow_rows = github_cli.json_call(
        "workflow", "list", *repo_args, "--json", "name"
    )
    prefixes = ["tend-", *extra_prefixes]
    workflows = github_cli.unique(
        row["name"]
        for row in workflow_rows
        if any(row["name"].startswith(prefix) for prefix in prefixes)
    )

    runs_by_id: dict[int, dict[str, Any]] = {}
    for workflow in workflows:
        try:
            rows = github_cli.json_call(
                "run",
                "list",
                *repo_args,
                "--workflow",
                workflow,
                "--created",
                f">={since}",
                "--status",
                "completed",
                "--json",
                "databaseId,createdAt,name",
                "--limit",
                str(RUN_LIMIT),
                quiet=True,
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            print(
                f"WARNING: 'gh run list' for '{workflow}' failed — its runs are "
                "absent from the totals below.",
                file=sys.stderr,
            )
            rows = []
        if len(rows) >= RUN_LIMIT:
            print(
                f"WARNING: '{workflow}' returned {RUN_LIMIT} runs, the Actions "
                "API's pagination ceiling — older runs in the window are unreachable "
                "and the totals below under-report it. Narrow HOURS to bring the "
                "window under the ceiling; raising RUN_LIMIT cannot help.",
                file=sys.stderr,
            )
        for row in rows:
            runs_by_id[int(row["databaseId"])] = row

    print(f"Downloading artifacts for {len(runs_by_id)} runs...", file=sys.stderr)
    jobs: list[dict[str, Any]] = []
    skipped = 0
    with tempfile.TemporaryDirectory() as workdir:
        root = Path(workdir)
        for run_id, run in runs_by_id.items():
            run_dir = root / str(run_id)
            try:
                github_cli.run(
                    "run",
                    "download",
                    str(run_id),
                    *repo_args,
                    "--pattern",
                    "claude-session-logs*",
                    "--dir",
                    str(run_dir),
                    quiet=True,
                )
                usage_files = list(run_dir.rglob("token-usage.json"))
                run_jobs = [
                    record for path in usage_files for record in _json_documents(path)
                ]
                if not run_jobs:
                    raise ValueError("no token usage records")
            except (
                subprocess.CalledProcessError,
                OSError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                if not isinstance(error, subprocess.CalledProcessError):
                    print(f"{run_id}: unreadable token usage: {error}", file=sys.stderr)
                skipped += 1
                continue
            stamp = {
                "run_id": run_id,
                "workflow": run["name"],
                "created_at": run["createdAt"],
            }
            jobs.extend({**record, **stamp} for record in run_jobs)

    report = build_report(jobs, skipped=skipped)
    github_cli.dump(report)
    sys.stderr.write(render_summary(report, since=since))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None

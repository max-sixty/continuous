# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Report token use and spend across recent Tend workflow runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import github_cli

RUN_LIMIT = 1000

REPORT_JQ = r"""
def sum(f): map(f) | add // 0;
def pick(f): map(f // empty) | first;

def run_entry:
  {
    run_id: .[0].run_id,
    _order: .[0]._order,
    workflow: .[0].workflow,
    created_at: .[0].created_at,
    repo: pick(.repo),
    event: pick(.event),
    number: pick(.number),
    head_sha: pick(.head_sha),
    input_tokens: sum(.input_tokens),
    output_tokens: sum(.output_tokens),
    cache_creation_input_tokens: sum(.cache_creation_input_tokens),
    cache_read_input_tokens: sum(.cache_read_input_tokens),
    turns: sum(.turns),
    cost_usd: sum(.cost_usd),
    partial: (map(.partial // false) | any)
  }
  | .subject = (
      if .number then "#\(.number)"
      elif .head_sha then .head_sha
      else "?"
      end
    );

def fmt:
  if . >= 1000000 then "\(. / 100000 | floor | . / 10)M"
  elif . >= 1000 then "\(. / 100 | floor | . / 10)K"
  else "\(.)"
  end;

def usd:
  ((. + 1e-12) * 100 | round / 100)
  | tostring
  | if test("\\.")
    then split(".") | "\(.[0]).\((.[1] + "00")[:2])"
    else . + ".00"
    end
  | "$" + .;

def floor_marker: if . then "+" else "" end;
def short: .[:12];
def pad($width): . + ((" " * ($width - length)) // "");

def table($rows):
  ($rows | transpose | map(map(length) | max)) as $widths
  | $rows
  | map(
      . as $row
      | [
          range(0; $row | length) as $index
          | $row[$index] | pad($widths[$index])
        ]
      | join("  ")
      | sub(" +$"; "")
    );

def rollup:
  {
    n: length,
    cost: sum(.cost_usd),
    partial: (map(.partial) | any),
    i: sum(.input_tokens),
    o: sum(.output_tokens),
    cc: sum(.cache_creation_input_tokens),
    cr: sum(.cache_read_input_tokens)
  };

def cost_cell: (.cost | usd) + (.partial | floor_marker);
def by_cost: sort_by(.cost) | reverse;
def subjects:
  group_by(.subject)
  | map({key: (.[0].subject | short), workflows: (map(.workflow) | unique | join(","))} + rollup);

def summary($since):
  .totals as $totals
  | .runs as $runs
  | [
      "",
      "\($runs | length) runs since \($since)",
      "Total cost: \($totals.cost_usd | usd)\($totals.partial_runs > 0 | floor_marker)"
        + (if $totals.partial_runs > 0
           then " (\($totals.partial_runs) of \($runs | length) runs cost-unknown)"
           else ""
           end),
      "Tokens: \($totals.input_tokens | fmt) in, \($totals.output_tokens | fmt) out, \($totals.cache_creation_input_tokens | fmt) cache-create, \($totals.cache_read_input_tokens | fmt) cache-read",
      ""
    ]
  + table(
      [["WORKFLOW", "RUNS", "COST", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ"]]
      + ($runs
         | group_by(.workflow)
         | map({key: .[0].workflow} + rollup)
         | by_cost
         | map([
             .key,
             (.n | tostring),
             cost_cell,
             (.i | fmt),
             (.o | fmt),
             (.cc | fmt),
             (.cr | fmt)
           ]))
    )
  + [""]
  + table(
      [["SUBJECT", "RUNS", "COST", "WORKFLOWS", "CACHE-READ"]]
      + ($runs
         | subjects
         | by_cost
         | .[:20]
         | map([.key, (.n | tostring), cost_cell, .workflows, (.cr | fmt)]))
    )
  + [""]
  + (if $totals.partial_runs > 0
     then table(
       [["COST-UNKNOWN", "RUNS", "CACHE-READ", "OUTPUT", "WORKFLOWS"]]
       + ($runs
          | map(select(.partial))
          | subjects
          | sort_by(.cr)
          | reverse
          | map([.key, (.n | tostring), (.cr | fmt), (.o | fmt), .workflows]))
     ) + [""]
     else []
     end)
  + table(
      [["RUN", "WORKFLOW", "SUBJECT", "COST", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ", "TIME"]]
      + ($runs
         | map([
             (.run_id | tostring),
             .workflow,
             (.subject | short),
             ((.cost_usd | usd) + (.partial | floor_marker)),
             (.input_tokens | fmt),
             (.output_tokens | fmt),
             (.cache_creation_input_tokens | fmt),
             (.cache_read_input_tokens | fmt),
             .created_at[:16]
           ]))
    )
  + [""]
  + (($runs | map(.subject) | unique | length) as $count
     | if $count > 20
       then ["Subjects: showing the 20 costliest of \($count); the JSON on stdout has them all."]
       else []
       end)
  + (if $totals.partial_runs > 0
     then ["COST-UNKNOWN lists the runs that emitted no result event, typically cancelled: their tokens are counted everywhere, their cost is not recoverable. A '+' marks a cost that is a floor rather than the spend."]
     else []
     end)
  + (if $totals.skipped_runs > 0
     then ["\($totals.skipped_runs) run(s) uploaded no readable claude-session-logs artifact and are absent entirely: codex-harness runs, runs that ended before the upload, and torn uploads."]
     else []
     end)
  + ["Cost at API list prices — a large multiple of the effective rate on Claude Code subscriptions."]
  | join("\n") + "\n";

. as $input
| ($input.jobs
   | to_entries
   | map(.value + {_order: .key})
   | group_by(.run_id)
   | map(run_entry)
   | sort_by(.created_at, ._order)
   | group_by(.created_at)
   | reverse
   | (add // [])
   | map(del(._order))) as $runs
| {
    runs: $runs,
    totals: ($runs | {
      input_tokens: sum(.input_tokens),
      output_tokens: sum(.output_tokens),
      cache_creation_input_tokens: sum(.cache_creation_input_tokens),
      cache_read_input_tokens: sum(.cache_read_input_tokens),
      turns: sum(.turns),
      cost_usd: (sum(.cost_usd) | (. + 1e-12) * 100 | round / 100),
      partial_runs: (map(select(.partial)) | length),
      skipped_runs: $input.skipped
    })
  } as $report
| {report: $report, summary: ($report | summary($input.since))}
"""


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


def _report(jobs: list[dict[str, Any]], *, skipped: int, since: str) -> dict[str, Any]:
    result = subprocess.run(
        ["jq", "-c", REPORT_JQ],
        input=json.dumps({"jobs": jobs, "skipped": skipped, "since": since}),
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


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
                "API's pagination ceiling — older runs in the window are "
                "unreachable and the totals below under-report it. Narrow HOURS "
                "to bring the window under the ceiling; raising RUN_LIMIT cannot help.",
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

    output = _report(jobs, skipped=skipped, since=since)
    github_cli.dump(output["report"])
    sys.stderr.write(output["summary"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None

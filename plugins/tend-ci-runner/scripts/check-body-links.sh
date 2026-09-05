#!/usr/bin/env bash
# Checks the GitHub links in a composed body before it is posted.
#
# Usage: check-body-links.sh <body-file>
#
# Two failure shapes, both of which ship a citation that resolves to nothing or
# to different code than the text describes, in the artifact a maintainer reads
# to decide whether to merge:
#
#   Fabricated SHA. The model sees an abbreviated OID in `git log` output and
#   extends it to 40 characters instead of running `git rev-parse HEAD`. The
#   result is well-formed, so any check that tests the *shape* of a permalink
#   passes it, and the link 404s from the moment it is posted. Only resolving
#   the OID separates a real commit from an invented one.
#
#   Un-pinned line anchor. `blob/main/...#L42` stays valid while the lines
#   underneath it move, so the link silently comes to point at other code. A
#   ref that is not a full 40-hex OID cannot pin a line.
#
# One API call per distinct (repo, SHA) pair, and bodies carry one or two. A
# hand-typed owner falls out of the same call: `repos/<wrong-owner>/<repo>/
# commits/<sha>` does not resolve either.
#
# A repo the token cannot read reports the same way a dead SHA does, so the
# message names both readings rather than asserting the link is dead. A commit
# this session made but has not pushed yet reports the same way too, and there
# it is right: the link would 404 for a reader. Run the check after the push.
#
# Exit codes:
#   0  every GitHub link in the body checks out
#   1  problems found, one per line on stdout
#   2  usage error

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: check-body-links.sh <body-file>" >&2
  exit 2
fi

body="$1"
if [ ! -f "$body" ]; then
  echo "check-body-links.sh: no such file: $body" >&2
  exit 2
fi

# A URL ends at the delimiters that surround it in markdown: `)` closes a link,
# `]`, quotes, backticks, angle brackets and whitespace end a bare one. The
# leading `]` inside the negated class is literal.
url_re='https?://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(blob|blame|tree|commit|commits|raw)/[^][)("<>`'"'"'[:space:]]+'

problems=$(mktemp)
shas=$(mktemp)
trap 'rm -f "$problems" "$shas"' EXIT

while IFS= read -r url; do
  [ -n "$url" ] || continue
  # https://github.com/OWNER/REPO/blob/REF/path -> fields 4, 5, 7
  read -r slug ref <<<"$(printf '%s\n' "$url" | awk -F/ '{print $4 "/" $5, $7}')"
  ref=${ref%%#*}
  # A bare URL in prose ends at the sentence, so trailing punctuation lands in
  # the ref of a `commit/<sha>` link and would skip the resolve entirely.
  ref=${ref%[.,;:!?]}
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s %s\n' "$slug" "$ref" >>"$shas"
  elif [[ "$url" == *"#L"* ]]; then
    printf 'un-pinned line link: ref `%s` is not a full commit SHA, so the lines it points at can move — %s\n' \
      "$ref" "$url" >>"$problems"
  fi
done < <(grep -oE "$url_re" "$body" || true)

while read -r slug sha; do
  [ -n "$sha" ] || continue
  gh api "repos/$slug/commits/$sha" --jq '.sha' >/dev/null 2>&1 && continue
  printf 'unresolvable SHA %s in %s — the commit does not exist (a hand-typed OID or a wrong owner), or the token cannot read that repo\n' \
    "$sha" "$slug" >>"$problems"
done < <(sort -u "$shas")

if [ -s "$problems" ]; then
  cat "$problems"
  exit 1
fi

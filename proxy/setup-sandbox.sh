#!/usr/bin/env bash
# Runner-side setup for credential isolation.
#
# Runs as the privileged `runner` user. Stands up everything needed to run the
# agent as a separate, non-sudo `tend-sandbox` user whose only path to an
# authenticated GitHub *or* Anthropic call is a local mitmproxy that holds the
# real secrets:
#
#   1. Create the tend-sandbox user (no sudo, distinct UID).
#   2. Neutralize the bot PAT actions/checkout persists for git — otherwise the
#      sandbox reads it off disk and isolation is moot.
#   3. Hand the checkout to tend-sandbox (and make the path traversable) so the
#      agent can edit and commit.
#   4. Start the injecting proxy (holds the real GitHub + Anthropic credentials
#      in its own memory) and system-trust its CA so the sandbox's gh/git accept
#      the intercepted TLS. (claude is Node and uses its own CA bundle, so the
#      agent step also points NODE_EXTRA_CA_CERTS at the exported PROXY_CA_CERT.)
#
# Exports for later steps via $GITHUB_ENV: SANDBOX, AGENT_HOME, PROXY_URL,
# TEND_RUN_DIR, PROXY_CA_CERT, AGENT_ENV_FILE, AGENT_PATH.
#
# Inputs (env): TEND_GH_TOKEN (real PAT), TEND_ANTHROPIC_OAUTH_TOKEN and/or
# TEND_ANTHROPIC_API_KEY (real Anthropic credential, injected for
# api.anthropic.com), ACTION_PATH (this action's checkout), MITMPROXY_VERSION
# (pinned mitmproxy version), TEND_UV_DIR (tend's own pinned uv, installed by
# shared/steps/install-proxy-uv.sh). GITHUB_WORKSPACE / RUNNER_TEMP /
# UV_CACHE_DIR come from Actions. Optional adopter levers (from
# .config/tend.yaml): TEND_SANDBOX_PATH
# (newline-separated dirs prepended to the sandbox PATH) and TEND_SANDBOX_ENV
# (newline-separated NAME=VALUE pairs added to the agent env; reserved keys
# rejected). TEND_SANDBOX_SETUP (commands) is consumed by the separate
# shared/steps/sandbox-setup.sh step, not here.
set -euo pipefail

SANDBOX=tend-sandbox
AGENT_HOME="/home/${SANDBOX}"
PROXY_PORT=8899
PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
# Public CA cert the proxy generates; system-trusted below for gh/git and
# pointed at by NODE_EXTRA_CA_CERTS for claude. World-readable so the sandbox
# (a different UID) can read it.
PROXY_CA_CERT=/usr/local/share/ca-certificates/tend-proxy.crt
# Run dir (sentinels, PTY log, wrapper) lives in the sandbox's own home so it
# can write there freely; the runner reads it via the 0755 home path. (Under
# RUNNER_TEMP the sandbox can't create it — that dir is runner-owned.)
TEND_RUN_DIR="${AGENT_HOME}/run"
CONFDIR="${RUNNER_TEMP}/tend-proxy"

log() { echo "[setup-sandbox] $*"; }

if [ -z "${TEND_GH_TOKEN:-}" ]; then
  echo "::error::TEND_GH_TOKEN is unset; cannot start the credential proxy"
  exit 1
fi
# The Anthropic credential is gated upstream by the action's "Validate auth
# configured" step and enforced at the point of use by the addon constructor
# (inject_credentials.py raises if neither scheme is set), so it is not
# re-checked here.
if [ -z "${MITMPROXY_VERSION:-}" ]; then
  echo "::error::MITMPROXY_VERSION is unset; the action must pin it"
  exit 1
fi

# 1. Non-sudo sandbox user. -m gives it /home/tend-sandbox (0755, so the
#    runner can still read the session logs it writes).
if ! id "$SANDBOX" >/dev/null 2>&1; then
  sudo useradd -m -s /usr/bin/bash "$SANDBOX"
fi
log "user $SANDBOX uid=$(id -u "$SANDBOX")"

# A global ignore for the one file the harness leaves in the checkout. The Run
# Claude step writes `.claude/settings.local.json` into the workspace, untracked
# and unignored, next to the `.claude/skills/` adopters do track — so a session
# that stages broadly (`git add -A`) sweeps `defaultMode: bypassPermissions`
# into the PR it opens. Global rather than the checkout's `info/exclude`: it
# covers every repo and worktree the agent touches, and writes nothing into the
# adopter's `.git`. core.excludesFile is pinned rather than left to git's XDG
# default path, which a runner XDG_CONFIG_HOME leaking through sudo would move
# out from under it.
sudo -u "$SANDBOX" mkdir -p "${AGENT_HOME}/.config/git"
printf '/.claude/settings.local.json\n' \
  | sudo -u "$SANDBOX" tee "${AGENT_HOME}/.config/git/ignore" >/dev/null
# `git -C` because this runs before step 3 grants the sandbox traversal of the
# workspace: every sudo'd command inherits the runner's cwd there, and git stats
# its cwd on startup whatever the command — under a 0750 /home/runner that is
# `fatal: failed to stat`, exit 128, before the agent ever launches.
sudo -u "$SANDBOX" env HOME="$AGENT_HOME" XDG_CONFIG_HOME="${AGENT_HOME}/.config" \
  git -C "$AGENT_HOME" config --global core.excludesFile "${AGENT_HOME}/.config/git/ignore"
log "global gitignore at ${AGENT_HOME}/.config/git/ignore"

# Decide the Anthropic auth scheme ONCE, here: unset the losing variable so
# the proxy (which inherits this shell's env) can never disagree with the
# dummy the agent gets — the addon injects whichever scheme it sees set,
# and only one is set. OAuth wins, matching the action's input precedence.
if [ -n "${TEND_ANTHROPIC_OAUTH_TOKEN:-}" ]; then
  unset TEND_ANTHROPIC_API_KEY
  ANTHROPIC_DUMMY="CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-tendproxydummy0000000000000000000000000000"
else
  ANTHROPIC_DUMMY="ANTHROPIC_API_KEY=sk-ant-api03-tendproxydummy0000000000000000000000000000"
fi

# The agent's launch environment, one NAME=VALUE per line, consumed by every
# step that runs something as the sandbox user (mapfile -t + `env "${arr[@]}"`).
# One file so the plugin-install and Run Claude steps cannot drift. Contents:
# the proxy routing, CA trust for every client family (system store for
# gh/git/curl is implicit; NODE_EXTRA_CA_CERTS for claude (Node ignores the
# system store); SSL_CERT_FILE/REQUESTS_CA_BUNDLE for uv and certifi-based
# Python — all pointing at bundles that include the proxy CA once
# update-ca-certificates has run below), and the DUMMY credentials the proxy
# swaps for real ones (gh refuses to run with no token at all; claude emits
# the auth headers for whichever scheme is set). The `tendproxydummy` marker
# lets the smoke prove the real secrets never reach the agent.
# CLAUDE_CODE_REMOTE suppresses interactive prompts (auth confirmation,
# plugin-install confirmation) in every sandbox claude invocation.
# The XDG base dirs are pinned under the sandbox home: GitHub runners export
# XDG_CONFIG_HOME=/home/runner/.config (and may set the siblings), which leaks
# through sudo into the sandbox — uv would then write its receipt/cache and any
# XDG-aware tool its config under the runner's home, which the sandbox UID can't.
# Adopter PATH additions (`sandbox_path:` in .config/tend.yaml, threaded in as
# TEND_SANDBOX_PATH — one dir per line). These are the adopter's explicit opt-in
# and are prepended ahead of the auto-derived toolchains below (so their tools
# win), with a leading `~` expanded to the sandbox home (the adopter doesn't
# know the sandbox username).
#
# What this lever reaches is a dir under the sandbox home or a system location.
# Runner-home PATH entries are mirrored automatically below; naming the source
# runner-home path here would instead put the credential-holding home itself on
# the agent's PATH, so it remains refused.
runner_home="${HOME:-/home/runner}"
declare -a _extra_path=()
if [ -n "${TEND_SANDBOX_PATH:-}" ]; then
  while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    # Expand a leading literal `~` to the sandbox home (the case globs match the
    # tilde literally; they are not shell tilde expansion). SC2088 misreads this.
    # shellcheck disable=SC2088
    case "$dir" in "~") dir="$AGENT_HOME" ;; "~/"*) dir="${AGENT_HOME}/${dir#\~/}" ;; esac
    # These entries are prepended verbatim, so they are the one way a
    # runner-home dir could reach the agent's PATH — the rewrite below can't
    # catch what never passes through it. Refuse rather than drop: the config
    # asked for something this lever cannot do, and the fix is a different one.
    case "$dir" in
      "${runner_home}" | "${runner_home}"/*)
        echo "::error::sandbox_path entry '${dir}' is under the runner's home, which the agent's PATH never includes. Install or copy the tool into the sandbox with sandbox_setup: instead."
        exit 1
        ;;
    esac
    _extra_path+=("$dir")
  done <<<"$TEND_SANDBOX_PATH"
fi

AGENT_ENV_FILE="${RUNNER_TEMP}/tend-agent-env"
# Derive the sandbox PATH from the runner's own PATH rather than hardcoding a
# base set. setup-sandbox.sh runs as the privileged `runner` user, so $PATH
# here already holds every toolchain dir the runner image put on PATH — the
# /etc/skel-seeded language homes (.cargo/bin, .dotnet/tools, .config/composer/
# vendor/bin, …) plus system dirs (/opt/pipx_bin, /usr/local/.ghcup/bin,
# /usr/bin, …). `useradd -m` copied /etc/skel into the sandbox's OWN home, so
# each runner-home toolchain has a sibling under ${AGENT_HOME}. Before rewriting
# a runner-home entry, mirror its install root into that sibling. A PATH dir
# named `bin` gets its parent (so Python's ~/.local/bin also brings the
# ~/.local/lib it imports); another PATH dir gets its own tree. The runner copy
# replaces the /etc/skel copy, so a setup action's pinned tool cannot silently
# fall back to the image default.
#
# Security boundary: the PATH still carries no runner-home entry. Mirroring is
# limited to roots already announced on PATH, never the checkout, and only
# hard-links files that are world-readable and not world-writable. Directories
# are recreated sandbox-owned, so the agent may replace a tool in its own home
# but cannot change the runner's inode. A private credential file is absent by
# construction, not by a filename blocklist.
#
# Rust is the one companion tree: ~/.cargo/bin contains rustup shims, while the
# selected toolchain and default live in ~/.rustup. Mirroring only the PATH root
# would keep returning the sandbox image's toolchain after an adopter pinned a
# different one with dtolnay/rust-toolchain.
_world_readonly_file() {
  local mode
  mode=$(stat -c '%a' -- "$1") || return 1
  mode=$((8#${mode}))
  (( (mode & 0004) != 0 && (mode & 0002) == 0 ))
}

_world_readonly_dir() {
  local mode
  mode=$(stat -c '%a' -- "$1") || return 1
  mode=$((8#${mode}))
  (( (mode & 0005) == 0005 && (mode & 0002) == 0 ))
}

_mirror_runner_tree() {
  local source resolved relative destination stage item item_relative link_target link
  local mirrored
  source="$1"
  resolved=$(readlink -f -- "$source") || return 0
  [ -d "$resolved" ] || return 0
  case "$resolved" in
    "${runner_home}"/*) ;;
    *) return 0 ;;
  esac
  relative="${resolved#"${runner_home}"/}"
  destination="${AGENT_HOME}/${relative}"
  case "$destination" in
    "${AGENT_HOME}"/*) ;;
    *) echo "::error::refusing unsafe sandbox mirror destination '$destination'"; exit 1 ;;
  esac

  # The destination is a fresh useradd copy, not adopter state. Clear it so a
  # private runner file is absent rather than leaving a same-named skel default,
  # and so the runner's pinned version wins every collision.
  if [ -e "$destination" ] && [ ! -d "$destination" ]; then
    echo "::error::sandbox mirror destination is not a directory: $destination"
    exit 1
  fi
  sudo -u "$SANDBOX" mkdir -p "$destination"
  sudo find "$destination" -mindepth 1 -delete

  if ! _world_readonly_dir "$resolved"; then
    log "did not mirror private or writable runner tool root $resolved"
    return 0
  fi

  # Build the filtered tree in one runner-private staging dir, then cross the
  # privilege boundary once. `cp --link` makes this O(tree metadata) rather
  # than copying toolchains; the fallback covers separate self-hosted mounts.
  stage=$(mktemp -d "${RUNNER_TEMP}/tend-home-mirror.XXXXXX")
  if ! cp -a --link --no-preserve=ownership -- "$resolved/." "$stage/" 2>/dev/null; then
    find "$stage" -type d -exec chmod u+rwx {} +
    find "$stage" -mindepth 1 -delete
    cp -a --no-preserve=ownership -- "$resolved/." "$stage/"
  fi
  chmod 700 "$stage"
  # Directories are independent copies, so making them runner-writable cannot
  # affect the source. Remove whole subtrees whose source directory was not
  # already public; then remove individual private or writable files.
  find "$stage" -type d -exec chmod u+rwx {} +
  while IFS= read -r -d '' item; do
    item_relative="${item#"${resolved}"/}"
    if [ -d "$stage/$item_relative" ]; then
      find "$stage/$item_relative" -depth -delete
    fi
  done < <(
    find "$resolved" -mindepth 1 -type d \
      \( ! -perm -0005 -o -perm -0002 \) -prune -print0
  )
  find "$stage" -type f \
    \( ! -perm -0004 -o -perm -0002 -o -uid "$(id -u "$SANDBOX")" \) -delete

  # `cp -a` preserves symlinks without judging their targets. Keep only links
  # to the same public, read-only material, and retarget absolute runner-home
  # links to the corresponding sandbox tree.
  while IFS= read -r -d '' item; do
    item_relative="${item#"${stage}"/}"
    link_target=$(readlink -f -- "$resolved/$item_relative") || {
      unlink "$item"
      continue
    }
    if [ -f "$link_target" ]; then
      if ! _world_readonly_file "$link_target"; then unlink "$item"; continue; fi
    elif [ -d "$link_target" ]; then
      if ! _world_readonly_dir "$link_target"; then unlink "$item"; continue; fi
    else
      unlink "$item"
      continue
    fi
    link=$(readlink -- "$item")
    case "$link" in
      "${runner_home}") link="$AGENT_HOME" ;;
      "${runner_home}"/*) link="${AGENT_HOME}/${link#"${runner_home}"/}" ;;
    esac
    ln -sfnT -- "$link" "$item"
  done < <(find "$stage" -type l -print0)

  mirrored=$(find "$stage" -mindepth 1 | wc -l)
  if ! sudo cp -a --link --no-preserve=ownership -- "$stage/." "$destination/" 2>/dev/null; then
    sudo find "$destination" -mindepth 1 -delete
    sudo cp -a --no-preserve=ownership -- "$stage/." "$destination/"
  fi
  sudo find "$destination" -type d -exec chown "${SANDBOX}:${SANDBOX}" {} +
  find "$stage" -mindepth 1 -delete
  rmdir "$stage"
  log "mirrored $mirrored world-readable entries from $resolved"
}

IFS=: read -ra _runner_path <<<"${PATH}"
declare -A _mirrored_roots=()
for _d in "${_runner_path[@]}"; do
  [ -n "${_d}" ] || continue
  case "${_d}" in
    "${runner_home}" | "${runner_home}"/*) ;;
    *) continue ;;
  esac
  _resolved_path=$(readlink -f -- "${_d}") || continue
  case "${_resolved_path}" in
    "${runner_home}"/*) ;;
    *) continue ;;
  esac
  # A repo may deliberately add its own bin dir to PATH, but copying the whole
  # checkout would duplicate untrusted project code. Step 3 hands that tree to
  # the sandbox separately; sandbox_path remains the explicit PATH lever.
  case "${_resolved_path}" in
    "${GITHUB_WORKSPACE}" | "${GITHUB_WORKSPACE}"/*) continue ;;
  esac
  if [ -n "${RUNNER_TEMP:-}" ]; then
    case "${_resolved_path}" in
      "${RUNNER_TEMP}") continue ;;
      "${RUNNER_TEMP}"/*) _root="${_resolved_path}" ;;
      */bin) _root="${_resolved_path%/bin}" ;;
      *) _root="${_resolved_path}" ;;
    esac
  else
    case "${_resolved_path}" in
      */bin) _root="${_resolved_path%/bin}" ;;
      *) _root="${_resolved_path}" ;;
    esac
  fi
  [ "${_root}" != "${runner_home}" ] || _root="${_resolved_path}"
  if [ -z "${_mirrored_roots[${_root}]:-}" ]; then
    _mirror_runner_tree "${_root}"
    _mirrored_roots["${_root}"]=1
  fi
  if [ "${_resolved_path}" = "${runner_home}/.cargo/bin" ] && \
     [ -d "${runner_home}/.rustup" ] && \
     [ -z "${_mirrored_roots[${runner_home}/.rustup]:-}" ]; then
    _mirror_runner_tree "${runner_home}/.rustup"
    _mirrored_roots["${runner_home}/.rustup"]=1
  fi
done

declare -A _seen_path=()
declare -a _agent_path=()
# Adopter-declared dirs win over everything, .local/bin included: prepend them
# first and verbatim (trusted opt-in, so not existence-tested like the derived
# entries below — the dir may be populated by a later `setup:` step) so an
# adopter dir can shadow a same-named binary in .local/bin.
for _d in ${_extra_path[@]+"${_extra_path[@]}"}; do
  [ -n "${_seen_path[${_d}]:-}" ] && continue
  _agent_path+=("${_d}")
  _seen_path["${_d}"]=1
done
# .local/bin next, ahead of the auto-derived toolchains below.
if [ -z "${_seen_path["${AGENT_HOME}/.local/bin"]:-}" ]; then
  _agent_path+=("${AGENT_HOME}/.local/bin")
  _seen_path["${AGENT_HOME}/.local/bin"]=1
fi
for _d in "${_runner_path[@]}"; do
  [ -n "${_d}" ] || continue
  case "${_d}" in
    "${runner_home}") continue ;;                                     # never expose the runner home root
    "${runner_home}"/*) _d="${AGENT_HOME}/${_d#"${runner_home}"/}" ;; # rewrite to the sandbox's own copy
  esac
  [ -n "${_seen_path[${_d}]:-}" ] && continue                           # dedup
  if [ -d "${_d}" ] && sudo -u "${SANDBOX}" test -x "${_d}"; then
    _agent_path+=("${_d}")
    _seen_path["${_d}"]=1
  fi
done
# Guarantee the base system dirs even if the runner PATH somehow lacks them.
for _d in /usr/local/bin /usr/bin /bin; do
  [ -n "${_seen_path[${_d}]:-}" ] && continue
  _agent_path+=("${_d}")
  _seen_path["${_d}"]=1
done
AGENT_PATH="$(IFS=:; printf '%s' "${_agent_path[*]}")"
# The composed PATH, in the log: it is the only place the tilde expansion and
# the runner-home rewrite are visible, and both are silent when they don't find
# what an adopter expected. sandbox-setup.sh reports the consequence — which
# commands the runner resolves and the agent can't — after sandbox_setup runs.
log "sandbox PATH: ${AGENT_PATH}"
cat >"$AGENT_ENV_FILE" <<EOF
HOME=${AGENT_HOME}
PATH=${AGENT_PATH}
XDG_CONFIG_HOME=${AGENT_HOME}/.config
XDG_CACHE_HOME=${AGENT_HOME}/.cache
XDG_DATA_HOME=${AGENT_HOME}/.local/share
XDG_STATE_HOME=${AGENT_HOME}/.local/state
HTTPS_PROXY=${PROXY_URL}
HTTP_PROXY=${PROXY_URL}
https_proxy=${PROXY_URL}
http_proxy=${PROXY_URL}
NO_PROXY=localhost,127.0.0.1
no_proxy=localhost,127.0.0.1
NODE_EXTRA_CA_CERTS=${PROXY_CA_CERT}
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
GH_TOKEN=ghp_tendproxydummy000000000000000000000
GITHUB_TOKEN=ghp_tendproxydummy000000000000000000000
CLAUDE_CODE_REMOTE=1
${ANTHROPIC_DUMMY}
EOF

# Adopter env additions (`sandbox_env:` in .config/tend.yaml, threaded in as
# TEND_SANDBOX_ENV — one NAME=VALUE per line). Appended after the fixed block
# (later duplicates win under `env "${arr[@]}"`). The generator already rejects
# reserved names (proxy routing, CA trust, dummy credentials) at `init`; this
# re-checks them here so a hand-edited workflow can't smuggle a routing/cred
# override past the security boundary. Keep this case in sync with
# RESERVED_SANDBOX_ENV in generator/src/tend/config.py — the
# `sandbox-env-reserved-parity` pre-commit hook fails the commit on drift.
if [ -n "${TEND_SANDBOX_ENV:-}" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    # Reject a line with no `=`: `${line%%=*}` would yield the whole token as
    # `name`, pass the reserved case, and get appended verbatim — `env` then
    # treats the assignment-less token as the command to exec and the agent
    # never launches. The generator only emits NAME=VALUE lines; this defends
    # a hand-edited workflow.
    case "$line" in
      *=*) ;;
      *)
        echo "::error::sandbox_env line is not NAME=VALUE: '$line'"
        exit 1
        ;;
    esac
    name="${line%%=*}"
    case "$name" in
      HOME|PATH|XDG_CONFIG_HOME|XDG_CACHE_HOME|XDG_DATA_HOME|XDG_STATE_HOME|\
      HTTPS_PROXY|HTTP_PROXY|https_proxy|http_proxy|NO_PROXY|no_proxy|\
      NODE_EXTRA_CA_CERTS|SSL_CERT_FILE|REQUESTS_CA_BUNDLE|\
      GH_TOKEN|GITHUB_TOKEN|CLAUDE_CODE_REMOTE|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)
        echo "::error::sandbox_env may not set reserved key '$name'"
        exit 1
        ;;
    esac
    printf '%s\n' "$line" >>"$AGENT_ENV_FILE"
  done <<<"$TEND_SANDBOX_ENV"
fi

# Export NOW, before any fallible step below — the if:always() ownership
# restore in the action keys on SANDBOX, and a proxy-startup failure after
# the workspace chown must still leave it set so the restore runs.
{
  echo "SANDBOX=${SANDBOX}"
  echo "AGENT_HOME=${AGENT_HOME}"
  echo "PROXY_URL=${PROXY_URL}"
  echo "TEND_RUN_DIR=${TEND_RUN_DIR}"
  echo "PROXY_CA_CERT=${PROXY_CA_CERT}"
  echo "AGENT_ENV_FILE=${AGENT_ENV_FILE}"
  echo "AGENT_PATH=${AGENT_PATH}"
} >>"$GITHUB_ENV"

# 2. Neutralize the credential actions/checkout persisted for git. Modern
#    checkout stores it in an external file referenced by an includeIf, not as
#    a plain extraheader. So: drop the extraheader (older form); unset each
#    includeIf key (--unset on the FULL key — --remove-section can't match the
#    dotted subsection name); and DELETE the external credential files. Deleting
#    (not chmod) is load-bearing: the PAT is gone from disk, and git silently
#    skips a now-missing include — whereas an unreadable include is *fatal* and
#    would break every agent git operation. The proxy is the only auth path left.
git -C "$GITHUB_WORKSPACE" config --local --unset-all \
  'http.https://github.com/.extraheader' 2>/dev/null || true
while read -r key; do
  [ -n "$key" ] || continue
  git -C "$GITHUB_WORKSPACE" config --local --unset "$key" 2>/dev/null || true
done < <(git -C "$GITHUB_WORKSPACE" config --local --name-only --get-regexp '^includeif\.' 2>/dev/null || true)
sudo find "$RUNNER_TEMP" -maxdepth 2 -name 'git-credentials-*' -delete 2>/dev/null || true

# Verify the strip actually worked (the load-bearing security step): fail loudly
# if any GitHub credential still resolves in the workspace config. The repo rule
# is that an unhandled format fails with a clear error, not silently.
if git -C "$GITHUB_WORKSPACE" config --local --list 2>/dev/null \
     | grep -qiE 'extraheader=|^includeif\.gitdir'; then
  echo "::error::failed to neutralize the persisted git credential in $GITHUB_WORKSPACE/.git/config"
  git -C "$GITHUB_WORKSPACE" config --local --list | grep -iE 'extraheader=|^includeif\.gitdir' || true
  exit 1
fi
log "neutralized persisted git credentials"

# 3. Make the path to the workspace traversable by the sandbox, then hand it
#    the checkout so the agent can edit and commit. The sandbox owns the tree,
#    so no safe.directory entry is needed. Grant o+x (traversal only, not read)
#    on every ancestor of the workspace — derived, not hard-coded to /home/runner,
#    so it works wherever the runner places the checkout. Fine on a single-use runner.
parent="$(dirname "$GITHUB_WORKSPACE")"
while [ "$parent" != "/" ]; do
  sudo chmod o+x "$parent" 2>/dev/null || true
  parent="$(dirname "$parent")"
done
sudo chown -R "${SANDBOX}:${SANDBOX}" "$GITHUB_WORKSPACE"
sudo -u "$SANDBOX" test -r "$GITHUB_WORKSPACE/.git/config" \
  || { echo "::error::sandbox cannot access the workspace at $GITHUB_WORKSPACE"; exit 1; }
log "workspace handed to $SANDBOX"

# Shared dir the sandbox writes (sentinels, PTY log, wrapper) and the runner
# reads. Sandbox-owned so its hooks can touch the sentinels; the runner
# supervisor polls them via the 0755 home/temp path.
sudo -u "$SANDBOX" mkdir -p "$TEND_RUN_DIR"
log "run dir $TEND_RUN_DIR"

# 4. Start the injecting proxy. It inherits the real GitHub + Anthropic
#    credentials from this shell; they never leave this runner-owned process.
#    confdir is 0700 runner-only so the sandbox can't read the CA private key
#    (it only needs the public cert, added to the system trust store below).
mkdir -p "$CONFDIR"
chmod 700 "$CONFDIR"
# Warm the uvx cache first so the backgrounded launch starts immediately and
# the readiness wait below measures startup, not a cold dependency resolve.
# Pinned + UV_CACHE_DIR (set by the action) point at the actions/cache-backed
# dir, so this is a fast restore after the first run.
# $TEND_UV_DIR holds tend's own pinned uv (shared/steps/install-proxy-uv.sh),
# addressed absolutely rather than through PATH: the binary that launches the
# credential-holding process is tend's, not whatever the adopter's `setup:`
# left on PATH.
MITMPROXY="mitmproxy==${MITMPROXY_VERSION}"
UVX="${TEND_UV_DIR}/uvx"
"$UVX" --from "$MITMPROXY" mitmdump --version >/dev/null
log "starting proxy"
# The --allow-hosts regex scopes which hosts mitmproxy TLS-intercepts. It must
# cover every host the addon injects into — keep it in sync with the
# BASIC_HOSTS / TOKEN_HOSTS / ANTHROPIC_HOSTS frozensets in inject_credentials.py
# (which own the credential boundary). A host in those sets but missing here is
# never intercepted, so its dummy is never swapped for the real secret and auth
# fails with a 401.
nohup "$UVX" --from "$MITMPROXY" mitmdump \
  -s "${ACTION_PATH}/proxy/inject_credentials.py" \
  --listen-host 127.0.0.1 --listen-port "$PROXY_PORT" \
  --set confdir="$CONFDIR" \
  --allow-hosts '^((api\.|codeload\.|uploads\.)?github\.com|raw\.githubusercontent\.com|api\.anthropic\.com)(:[0-9]+)?$' \
  </dev/null >"${RUNNER_TEMP}/tend-proxy.log" 2>&1 &
PROXY_PID=$!
echo "$PROXY_PID" >"${RUNNER_TEMP}/tend-proxy.pid"
disown

# Wait for the proxy to accept a connection. The CA file is not the readiness
# signal: mitmdump writes it before it binds the port and before it loads `-s`
# scripts, so a port already in use, or an addon that raises on import, leaves
# the CA behind and exits ~0.1s later. Waiting on the file and then sampling
# the process is a race decided by how fast the addon imports, and losing it
# means trusting the CA, logging "proxy up", and launching the agent against a
# dead proxy — every authenticated call then fails with nothing to explain it.
# The liveness check inside the loop stops early rather than burning 30s.
PROXY_READY=
for _ in $(seq 1 60); do
  kill -0 "$PROXY_PID" 2>/dev/null || break
  if (exec 3<>"/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null; then
    PROXY_READY=1
    break
  fi
  sleep 0.5
done
if [ -z "$PROXY_READY" ]; then
  echo "::error::mitmdump never accepted a connection on ${PROXY_PORT}"
  cat "${RUNNER_TEMP}/tend-proxy.log" || true
  exit 1
fi
if [ ! -f "${CONFDIR}/mitmproxy-ca-cert.pem" ]; then
  echo "::error::proxy CA not generated; mitmdump failed to start"
  cat "${RUNNER_TEMP}/tend-proxy.log" || true
  exit 1
fi

# System-trust the proxy CA so the sandbox's gh (Go) and git (libcurl) accept
# the intercepted GitHub TLS. Only the public cert is exported. claude (Node)
# ignores the system store, so the agent step points NODE_EXTRA_CA_CERTS at this
# same cert for the intercepted api.anthropic.com TLS.
sudo cp "${CONFDIR}/mitmproxy-ca-cert.pem" "$PROXY_CA_CERT"
sudo update-ca-certificates >/dev/null
log "proxy up at $PROXY_URL; CA trusted"

log "done; agent runs as ${SANDBOX}, GitHub + Anthropic auth via the proxy"

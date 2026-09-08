#!/usr/bin/bash
set -euo pipefail

: "${SRT_VERSION:?SRT_VERSION is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${RUNNER_TOOL_CACHE:?RUNNER_TOOL_CACHE is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"

apparmor_userns=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
if [ -r "$apparmor_userns" ] && [ "$(/usr/bin/cat "$apparmor_userns")" = 1 ]; then
  if [ "${TEND_RUNNER_ENVIRONMENT:-}" = github-hosted ]; then
    # Record rollback before changing host state so cancellation or a failed
    # sysctl cannot leave an untracked policy change.
    echo "TEND_RESTORE_APPARMOR_USERNS=true" >> "$GITHUB_ENV"
    /usr/bin/sudo /usr/sbin/sysctl -q -w kernel.apparmor_restrict_unprivileged_userns=0
  else
    echo "::notice::Leaving self-hosted AppArmor policy unchanged; SRT will verify that bwrap and apply-seccomp have capability-bearing user namespaces"
  fi
fi

case "$(/usr/bin/uname -m)" in
  x86_64) srt_arch=x64 ;;
  aarch64) srt_arch=arm64 ;;
  *) echo "::error::SRT is unsupported on $(/usr/bin/uname -m)"; exit 1 ;;
esac

node_bin=$(/usr/bin/find "$RUNNER_TOOL_CACHE/node" -path "*/$srt_arch/bin/node" -type f -print \
  | /usr/bin/sort -V | /usr/bin/tail -n1)
if [ -z "$node_bin" ] || [ ! -x "$node_bin" ]; then
  echo "::error::No trusted x64 Node runtime found in RUNNER_TOOL_CACHE"
  exit 1
fi
npm_cli="${node_bin%/bin/node}/lib/node_modules/npm/bin/npm-cli.js"
if [ ! -f "$npm_cli" ]; then
  echo "::error::Trusted Node runtime has no npm sibling"
  exit 1
fi

missing=()
for command in /usr/bin/bwrap /usr/bin/socat /usr/bin/rg; do
  [ -x "$command" ] || missing+=("${command##*/}")
done
if [ "${#missing[@]}" -gt 0 ]; then
  /usr/bin/sudo /usr/bin/apt-get update
  /usr/bin/sudo /usr/bin/apt-get install -y bubblewrap socat ripgrep
fi

runtime_root=$(/usr/bin/mktemp -d /tmp/tend-runtime.XXXXXX)
srt_root="$runtime_root/srt"
npm_userconfig=$(/usr/bin/mktemp "$RUNNER_TEMP/tend-npm-user.XXXXXX")
npm_globalconfig=$(/usr/bin/mktemp "$RUNNER_TEMP/tend-npm-global.XXXXXX")
/usr/bin/env -i \
  PATH="${node_bin%/node}:/usr/sbin:/usr/bin:/sbin:/bin" HOME="$RUNNER_TEMP" \
  "$node_bin" "$npm_cli" install --prefix "$srt_root" \
    --userconfig "$npm_userconfig" --globalconfig "$npm_globalconfig" \
    --ignore-scripts --no-audit --no-fund \
    "@anthropic-ai/sandbox-runtime@$SRT_VERSION"
srt_package="$srt_root/node_modules/@anthropic-ai/sandbox-runtime"
srt_entry="$srt_package/dist/index.js"
srt_seccomp="$srt_package/vendor/seccomp/$srt_arch/apply-seccomp"
for command in "$srt_entry" "$srt_seccomp" /usr/bin/bwrap /usr/bin/socat /usr/bin/rg; do
  [ -e "$command" ] || { echo "::error::Missing SRT capability: $command"; exit 1; }
done
/usr/bin/chmod 755 "$runtime_root"
{
  echo "NODE_BIN=$node_bin"
  echo "TEND_RUNTIME_ROOT=$runtime_root"
  echo "TEND_NPM_CLI=$npm_cli"
  echo "TEND_NPM_USERCONFIG=$npm_userconfig"
  echo "TEND_NPM_GLOBALCONFIG=$npm_globalconfig"
  echo "TEND_SRT_ENTRY=$srt_entry"
  echo "TEND_SRT_SECCOMP=$srt_seccomp"
} >> "$GITHUB_ENV"

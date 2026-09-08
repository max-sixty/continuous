#!/usr/bin/bash
set -euo pipefail

if [ "${TEND_RESTORE_APPARMOR_USERNS:-}" = true ]; then
  /usr/bin/sudo /usr/sbin/sysctl -q -w kernel.apparmor_restrict_unprivileged_userns=1
fi

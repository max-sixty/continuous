# Codex Cloud with Worktrunk

Tend's Cloud setup lives in `environment.sh`. It installs the pinned Worktrunk
release, approves every command `.config/wt.toml` declares, syncs all project
dependencies, and installs `pre-commit` without installing Git hooks.

## Create the environment

Land this directory, `.config/wt.toml`, and `AGENTS.md` on the repository's
default branch before creating the environment. Then open
**Codex settings → Environments**, create an environment for `max-sixty/tend`,
and use these settings:

| Setting | Value |
|---|---|
| Container image | `universal` |
| Container caching | On |
| Setup script | `bash .config/codex-cloud/environment.sh` |
| Maintenance script | `bash .config/codex-cloud/environment.sh` |
| Agent internet access | On |
| Domain allowlist | All (unrestricted) |
| Allowed HTTP methods | All methods |

The environment needs no variables or secrets. Save it, start a Cloud task on
the desired branch, and validate the installation with:

```text
Run command -v wt, wt --version, wt config approvals list, wt test, and
pre-commit --version. Require every command to pass and do not modify files.
```

## Keep it current

`environment.sh` is the one setup command for both fresh and cached containers.
Worktrunk records the approvals itself, so a `.config/wt.toml` change needs no
edit here. Reset the environment cache when a repository change makes the
cached container incompatible.

The script is Cloud-specific: it approves this repo's `wt` commands without
review. Do not use it as local workstation setup.

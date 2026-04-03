---
name: test-discovery
description: Test skill for verifying repo-local skill auto-discovery in CI. Use when testing skill discovery.
---

# Test Discovery

This is a test skill to verify whether repo-local skills with proper YAML frontmatter are
auto-discovered by Claude Code in CI environments (via claude-code-action).

If this skill appears in the "following skills are available" system-reminder listing, auto-discovery
works. If not, there is a gap in CI skill discovery that needs investigation.

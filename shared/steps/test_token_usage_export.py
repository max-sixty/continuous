"""Security contracts for the quiescent agent-tree exporter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import token_usage
from _safe_files import read_regular_nofollow


def test_privileged_copy_exports_only_regular_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested/session.jsonl").write_text("{}\n")
    (source / "secret-link").symlink_to("/etc/passwd")
    os.mkfifo(source / "blocking-fifo")
    destination = tmp_path / "destination"

    token_usage.privileged_copy(source, destination, uid=os.getuid(), gid=os.getgid())

    assert (destination / "nested/session.jsonl").read_text() == "{}\n"
    assert not (destination / "secret-link").exists()
    assert not (destination / "blocking-fifo").exists()


def test_privileged_copy_refuses_symlinked_source_component(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)

    with pytest.raises(OSError):
        token_usage.privileged_copy(
            alias, tmp_path / "destination", uid=os.getuid(), gid=os.getgid()
        )


def test_privileged_copy_enforces_total_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "too-large").write_bytes(b"12345")
    monkeypatch.setattr(token_usage, "EXPORT_MAX_TOTAL_BYTES", 4)

    with pytest.raises(ValueError, match="byte limit"):
        token_usage.privileged_copy(
            source, tmp_path / "destination", uid=os.getuid(), gid=os.getgid()
        )


def test_fixed_export_read_is_bounded_and_does_not_follow_links(tmp_path: Path) -> None:
    regular = tmp_path / "message"
    regular.write_bytes(b"result")
    assert read_regular_nofollow(regular, max_bytes=6) == b"result"
    with pytest.raises(ValueError, match="exceeds"):
        read_regular_nofollow(regular, max_bytes=5)

    alias = tmp_path / "alias"
    alias.symlink_to(regular)
    with pytest.raises(OSError):
        read_regular_nofollow(alias, max_bytes=100)

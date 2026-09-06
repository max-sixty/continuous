from __future__ import annotations

import json
from pathlib import Path

import gist_memory
import pytest
from _fakes import FakeGh

REPOSITORY = "owner/repo"
GIST_ID = "abc123"
GIST_OWNER = "tend-bot"
BASELINE_KEY = "ab" * 32


def _gist(files: dict[str, str], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": GIST_ID,
        "description": f"tend auto memory: {REPOSITORY}",
        "public": False,
        "truncated": False,
        "owner": {"login": GIST_OWNER},
        "files": {
            name: {"content": content, "truncated": False}
            for name, content in files.items()
        },
    }
    value.update(overrides)
    return value


def _serve(
    fake_gh: FakeGh,
    files: dict[str, str],
    *,
    visibility: str = "public",
    **gist_overrides: object,
) -> None:
    fake_gh.respond("api", f"/repos/{REPOSITORY}", with_={"visibility": visibility})
    fake_gh.respond("api", f"/gists/{GIST_ID}", with_=_gist(files, **gist_overrides))


def test_restore_builds_claudes_real_auto_memory_directory(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    memory = tmp_path / "memory"
    files = {
        "MEMORY.md": "# Memory\n\n- Debugging notes: @debugging.md\n",
        "debugging.md": "# Debugging\n\nUse the integration fixture.\n",
    }
    _serve(fake_gh, files)

    assert (
        gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY) == 0
    )

    assert (memory / "MEMORY.md").read_text() == files["MEMORY.md"]
    assert (memory / "debugging.md").read_text() == files["debugging.md"]
    baseline = json.loads((memory / gist_memory.BASELINE_FILE).read_text())
    assert baseline["repository"] == REPOSITORY
    assert baseline["files"] == files
    assert baseline["signature"]
    assert GIST_ID not in (memory / gist_memory.BASELINE_FILE).read_text()
    assert json.loads((memory / gist_memory.SETTINGS_FILE).read_text()) == {
        "autoMemoryDirectory": str(memory),
        "autoMemoryEnabled": True,
    }


def test_cli_reads_gist_locator_and_baseline_key_from_the_environment(
    tmp_path: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = tmp_path / "memory"
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    for name, value in {
        "GITHUB_TOKEN": "not-a-real-token",
        "GITHUB_REPOSITORY": REPOSITORY,
        "TEND_MEMORY_GIST_ID": GIST_ID,
        "TEND_AUTO_MEMORY_GIST_OWNER": GIST_OWNER,
        "TEND_AUTO_MEMORY_DIRECTORY": str(memory),
        "TEND_AUTO_MEMORY_BASELINE_KEY": BASELINE_KEY,
    }.items():
        monkeypatch.setenv(name, value)

    assert gist_memory.main(["restore"]) == 0
    assert (memory / "MEMORY.md").read_text() == "# Memory\n"


@pytest.mark.parametrize(
    ("visibility", "overrides"),
    [
        ("private", {}),
        ("public", {"public": True}),
        ("public", {"description": "memory for a different repository"}),
        ("public", {"truncated": True}),
        ("public", {"files": {}}),
    ],
    ids=["private-repository", "public-gist", "wrong-repo", "truncated", "no-index"],
)
def test_restore_refuses_memory_that_is_not_safe_to_recall(
    tmp_path: Path,
    fake_gh: FakeGh,
    visibility: str,
    overrides: dict[str, object],
) -> None:
    memory = tmp_path / "memory"
    files = overrides.pop("files", {"MEMORY.md": "# Memory\n"})
    assert isinstance(files, dict)
    _serve(fake_gh, files, visibility=visibility, **overrides)

    with pytest.raises(gist_memory.GistMemoryError):
        gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)

    assert not memory.exists()


def test_save_skips_the_entire_change_set_when_one_file_conflicts(
    tmp_path: Path, fake_gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    memory = tmp_path / "memory"
    original = {
        "MEMORY.md": "# Memory\n\nOld index.\n",
        "debugging.md": "Old debugging note.\n",
        "obsolete.md": "Remove me.\n",
    }
    _serve(fake_gh, original)
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)

    (memory / "MEMORY.md").write_text("# Memory\n\nNew local index.\n")
    (memory / "debugging.md").write_text("Conflicting local note.\n")
    (memory / "patterns.md").write_text("New local topic.\n")
    (memory / "obsolete.md").unlink()

    remote = {
        **original,
        "debugging.md": "Concurrent remote note.\n",
        "remote-only.md": "Another run added this.\n",
    }
    _serve(fake_gh, remote)
    assert gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY) == 0

    assert not fake_gh.called("api", f"/gists/{GIST_ID}", "-X", "PATCH")
    captured = capsys.readouterr()
    assert "skipped entire save" in captured.err
    assert "debugging.md" in captured.err
    assert "saved 0 file(s)" in captured.out


def test_save_patches_one_consistent_change_set(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    memory = tmp_path / "memory"
    original = {
        "MEMORY.md": "# Memory\n\nOld index.\n",
        "obsolete.md": "Remove me.\n",
    }
    _serve(fake_gh, original)
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)

    (memory / "MEMORY.md").write_text("# Memory\n\nNew index.\n")
    (memory / "patterns.md").write_text("New topic.\n")
    (memory / "obsolete.md").unlink()
    _serve(fake_gh, original)
    fake_gh.respond("api", f"/gists/{GIST_ID}", "-X", "PATCH", with_="")

    assert gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY) == 0

    patch_call = fake_gh.calls.index(
        ("api", f"/gists/{GIST_ID}", "-X", "PATCH", "--input", "-")
    )
    assert json.loads(fake_gh.stdins[patch_call] or "") == {
        "files": {
            "MEMORY.md": {"content": "# Memory\n\nNew index.\n"},
            "obsolete.md": None,
            "patterns.md": {"content": "New topic.\n"},
        }
    }


def test_save_refuses_a_baseline_modified_by_the_agent(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    memory = tmp_path / "memory"
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)
    baseline_path = memory / gist_memory.BASELINE_FILE
    baseline = json.loads(baseline_path.read_text())
    baseline["files"]["MEMORY.md"] = "invented baseline"
    baseline_path.write_text(json.dumps(baseline))

    with pytest.raises(gist_memory.GistMemoryError, match="modified"):
        gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)


def test_save_refuses_to_follow_a_memory_symlink(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    memory = tmp_path / "memory"
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)
    outside = tmp_path / "outside.md"
    outside.write_text("must not leave this machine\n")
    (memory / "linked.md").symlink_to(outside)

    with pytest.raises(gist_memory.GistMemoryError, match="symlink"):
        gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)


def test_save_checks_the_directory_before_reading_its_baseline(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    actual = tmp_path / "moved-memory"
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, actual, BASELINE_KEY)
    memory = tmp_path / "memory"
    memory.symlink_to(actual, target_is_directory=True)

    with pytest.raises(
        gist_memory.GistMemoryError, match="directory must not be a symlink"
    ):
        gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)

    assert fake_gh.calls[-1] == ("api", f"/repos/{REPOSITORY}")


def test_save_refuses_to_silently_drop_nested_memory(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    memory = tmp_path / "memory"
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)
    topic = memory / "feedback"
    topic.mkdir()
    (topic / "testing.md").write_text("Nested topic.\n")

    with pytest.raises(gist_memory.GistMemoryError, match="nested"):
        gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)


def test_save_accepts_a_safe_unicode_topic_name(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    memory = tmp_path / "memory"
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    gist_memory.restore(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY)
    (memory / "débogage.md").write_text("Unicode topic.\n")
    _serve(fake_gh, {"MEMORY.md": "# Memory\n"})
    fake_gh.respond("api", f"/gists/{GIST_ID}", "-X", "PATCH", with_="")

    assert gist_memory.save(GIST_ID, REPOSITORY, GIST_OWNER, memory, BASELINE_KEY) == 0
    patch_call = fake_gh.calls.index(
        ("api", f"/gists/{GIST_ID}", "-X", "PATCH", "--input", "-")
    )
    assert json.loads(fake_gh.stdins[patch_call] or "") == {
        "files": {"débogage.md": {"content": "Unicode topic.\n"}}
    }

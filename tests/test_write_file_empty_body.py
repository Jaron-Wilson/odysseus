"""write_file: an empty body must not silently truncate a file that holds data,
and a write must never leave a half-written file at the target path.

Context: a model call whose arguments lost their content section (a parser
failure) used to reach WriteFileTool with an empty body; the existing file was
opened in "w" mode and the tool answered exit_code=0 with "Wrote 0 bytes",
destroying the file. Each "is refused" test below measures that the bytes at
the path are still there afterwards; each "still works" test guards a write
path this change must not narrow.
"""
import os
import tempfile

import pytest

from src import tool_execution as te
from src.agent_tools import ToolBlock
from src.agent_tools.filesystem_tools import EditFileTool, WriteFileTool

RECIPE = "# Classic banana cake\n\nMash 3 bananas. Bake 180C for 1 hour.\n"


@pytest.fixture
def target():
    """A fresh directory under the system temp root, which _tool_path_roots allows."""
    with tempfile.TemporaryDirectory(prefix="odysseus-write-file-") as directory:
        yield os.path.join(directory, "classic-banana-cake.md")


def _seed(path, text=RECIPE):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _call(path, body=None):
    """The write_file text form: first line is the path, the rest is the content."""
    return path if body is None else f"{path}\n{body}"


# -- The truncation this guards against ---------------------------------------

async def test_empty_body_after_the_path_line_is_refused_and_the_file_survives(target):
    _seed(target)
    res = await WriteFileTool().execute(_call(target, ""), {})
    assert res["exit_code"] == 1, res
    assert _read(target) == RECIPE


async def test_path_only_call_with_no_content_section_is_refused(target):
    """`lines[1] if len(lines) > 1 else ""` has two producers; this is the
    no-newline one."""
    _seed(target)
    res = await WriteFileTool().execute(_call(target), {})
    assert res["exit_code"] == 1, res
    assert _read(target) == RECIPE


async def test_whitespace_only_body_is_refused(target):
    """A body that carries no real characters is the same failure with padding
    left in."""
    _seed(target)
    res = await WriteFileTool().execute(_call(target, "   \n  "), {})
    assert res["exit_code"] == 1, res
    assert _read(target) == RECIPE


async def test_non_utf8_target_is_refused_on_its_size_not_on_the_decoded_read(target):
    """The existing read swallows UnicodeDecodeError and answers "", which would
    let a binary or latin-1 file look empty to the guard while holding real
    bytes."""
    with open(target, "wb") as handle:
        handle.write(b"\xc3\xa9\xe8\xaf\xad\xff\xfe\x00binary-ish payload")
    before = os.path.getsize(target)
    assert before > 0
    res = await WriteFileTool().execute(_call(target, ""), {})
    assert res["exit_code"] == 1, res
    assert os.path.getsize(target) == before


async def test_refusal_creates_no_extra_files_next_to_the_target(target):
    _seed(target)
    directory = os.path.dirname(target)
    res = await WriteFileTool().execute(_call(target, ""), {})
    assert res["exit_code"] == 1, res
    assert os.listdir(directory) == [os.path.basename(target)]


async def test_refusal_names_the_byte_count_and_points_at_edit_file(target):
    _seed(target)
    res = await WriteFileTool().execute(_call(target, ""), {})
    error = res.get("error", "")
    assert str(len(RECIPE)) in error, error
    assert "edit_file" in error, error
    assert "output" not in res, res


# -- Deliberate writes this change must keep working --------------------------

async def test_empty_body_on_a_new_path_still_creates_an_empty_file(target):
    res = await WriteFileTool().execute(_call(target, ""), {})
    assert res["exit_code"] == 0, res
    assert os.path.isfile(target) and os.path.getsize(target) == 0


async def test_whitespace_only_body_on_a_new_path_preserves_the_requested_content(target):
    whitespace = "   \n\t"
    res = await WriteFileTool().execute(_call(target, whitespace), {})
    assert res["exit_code"] == 0, res
    assert _read(target) == whitespace


async def test_empty_body_over_an_already_empty_file_succeeds(target):
    """Nothing is at risk, so the guard has nothing to refuse."""
    _seed(target, "")
    res = await WriteFileTool().execute(_call(target, ""), {})
    assert res["exit_code"] == 0, res
    assert os.path.getsize(target) == 0


async def test_a_real_body_still_writes_and_reports_a_diff(target):
    _seed(target)
    replacement = "# Classic banana cake\n\nMash 4 bananas.\n"
    res = await WriteFileTool().execute(_call(target, replacement), {})
    assert res["exit_code"] == 0, res
    assert _read(target) == replacement
    assert res["diff"]["added"] == 1 and res["diff"]["removed"] == 1


async def test_edit_file_remains_an_explicit_way_to_clear_a_file(target):
    """The route this change leaves open for clearing a file on purpose: replace
    the whole content with nothing."""
    _seed(target)
    import json
    res = await EditFileTool().execute(
        json.dumps({"path": target, "old_string": RECIPE, "new_string": ""}), {}
    )
    assert res["exit_code"] == 0, res
    assert os.path.getsize(target) == 0


# -- Atomic write: no half-written file at the target path ---------------------

async def test_write_is_staged_and_renamed_not_written_in_place(target, monkeypatch):
    """A crash mid-write must not leave a corrupt/partial file at `path` — the
    write goes to a sibling temp file and is renamed into place."""
    _seed(target)
    seen_paths = []
    real_open = open

    def _tracking_open(path, mode="r", *args, **kwargs):
        if isinstance(path, str) and os.path.dirname(path) == os.path.dirname(target) and "w" in mode:
            seen_paths.append(path)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _tracking_open)
    replacement = "fresh content\n"
    res = await WriteFileTool().execute(_call(target, replacement), {})
    assert res["exit_code"] == 0, res
    assert _read(target) == replacement
    # The write must never open the final target path itself in a writing mode:
    # every "w" open observed must be a sibling temp file, not `target`.
    assert target not in seen_paths, seen_paths


async def test_crash_mid_write_leaves_the_original_file_intact(target, monkeypatch):
    """Simulates a crash partway through the write: the temp file never gets
    renamed into place, so the original content at `path` must survive untouched."""
    _seed(target)

    def _boom(*a, **k):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "replace", _boom)
    res = await WriteFileTool().execute(_call(target, "new content\n"), {})
    assert res["exit_code"] == 1, res
    assert _read(target) == RECIPE
    # No leftover temp file next to the target.
    leftovers = [f for f in os.listdir(os.path.dirname(target))
                 if f != os.path.basename(target)]
    assert leftovers == [], leftovers


# -- The live dispatch path, not just the handler ------------------------------

async def test_execute_tool_block_refuses_a_lost_body_without_touching_the_file(target, monkeypatch):
    _seed(target)
    desc, result = await te.execute_tool_block(
        ToolBlock("write_file", _call(target, "")),
        owner="admin",
    )
    assert result.get("exit_code") == 1, result
    assert _read(target) == RECIPE

"""PF-R1/PF-R3/PF-R6: standalone builders keep file transport safe."""

import os
import stat
from pathlib import Path

import pytest

from multiagents import providers
from multiagents.agentwrap import read_prompt
from multiagents.providers import Provider


def file_provider():
    return Provider("custom", "native", {
        "prompt_transport": "file", "args": ["--input", "{prompt_file}"]}, {})


def test_standalone_builder_keeps_large_exact_input_in_distinct_private_files():
    provider = file_provider()
    text = "  {prompt_file} $(literal) é 😀\r\n" * 8192 + "\n\n"
    first = provider.build_command(prompt=text, model="m", workdir="/unused")
    second = provider.build_command(prompt="next\n", model="m", workdir="/unused")
    p0, p1 = Path(first[-1]), Path(second[-1])
    try:
        assert p0 != p1
        assert read_prompt(str(p0), 16 * 1024 * 1024) == text.encode()
        assert p1.read_bytes() == b"next\n"
        assert stat.S_IMODE(p0.stat().st_mode) == 0o600
        assert text not in first
    finally:
        p0.unlink()
        p1.unlink()


def test_explicit_file_needs_no_standalone_allocation(tmp_path, monkeypatch):
    def unexpected(_prompt):
        pytest.fail("allocated standalone input despite explicit launch file")

    monkeypatch.setattr(providers, "_standalone_prompt_file", unexpected)
    path = tmp_path / "prompt.md"
    path.write_bytes(b"input")
    argv = file_provider().build_command(
        prompt="input", prompt_file=str(path), model="m", workdir=str(tmp_path))
    assert argv == ["native", "--input", str(path)]


def test_standalone_builder_refuses_input_over_the_default_byte_bound():
    with pytest.raises(ValueError, match="prompt_file_max_bytes"):
        file_provider().build_command(
            prompt="é" * (8 * 1024 * 1024 + 1), model="m", workdir="/unused")


def test_consumer_detects_growth_past_the_bound(tmp_path, monkeypatch):
    path = tmp_path / "prompt.md"
    path.write_bytes(b"abcd")
    original = os.read
    appended = False

    def growing(fd, n):
        nonlocal appended
        data = original(fd, n)
        if data and not appended:
            with path.open("ab") as out:
                out.write(b"e")
            appended = True
        return data

    monkeypatch.setattr(os, "read", growing)
    with pytest.raises(ValueError, match="prompt_file_max_bytes"):
        read_prompt(str(path), 4)

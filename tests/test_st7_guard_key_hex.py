"""ST7: guard hex keys decode consistently on supported Python versions."""

from multiagents import git_guard


def test_hex_key_file_passes_ascii_text_to_fromhex(tmp_path, monkeypatch):
    class TextOnlyBytes(bytes):
        @classmethod
        def fromhex(cls, text):
            assert isinstance(text, str)
            return bytes.fromhex(text)

    monkeypatch.setattr(git_guard, "bytes", TextOnlyBytes, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key_path = git_guard.guard_key_path()
    key_path.parent.mkdir(mode=0o700)
    expected = bytes(range(32))
    key_path.write_bytes(expected.hex().encode("ascii") + b"\n")
    key_path.chmod(0o600)

    assert git_guard.ensure_guard_key() == expected

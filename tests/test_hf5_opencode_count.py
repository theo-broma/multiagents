"""HF5.0–HF5.3 / F135: public opencode.sh check count contract.

The section/row/footer shape follows ProvidersListCommand in upstream
https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/providers.ts
(read 2026-10-13): Credentials intro, provider/type rows, N credentials
outro; then Environment intro, provider/key rows, N environment variable(s).
Counts and provider names below are synthetic; no real CLI or account is used.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c2_harness as h  # noqa: E402

CHECK_TIMEOUT_SECONDS = 3


def check(tmp_path: Path, output: str, *, status: int = 0):
    listing = tmp_path / "providers-list.txt"
    listing.write_text(output)
    binary = tmp_path / "opencode"
    binary.write_text(
        '#!/bin/sh\n[ "$1" = providers ] && [ "$2" = list ] || exit 64\n'
        f"cat {shlex.quote(str(listing))}\nexit {status}\n"
    )
    binary.chmod(0o755)
    return h.run_shipped_script(
        "opencode", "check",
        {"MULTIAGENTS_BIN": str(binary), "MULTIAGENTS_OPENCODE_PLAN": ""},
        timeout=CHECK_TIMEOUT_SECONDS,
    )


def sections(total: str | None, *, provider: str = "Example api",
             environment: str = "Example EXAMPLE_API_KEY", env_count: int = 1):
    credentials = "\n┌ Credentials ~/.local/share/opencode/auth.json\n│\n"
    credentials += f"● {provider}\n│\n"
    if total is not None:
        credentials += f"└ {total}\n"
    return credentials + (
        f"\n┌ Environment\n│\n● {environment}\n│\n"
        f"└ {env_count} environment variable{'s' if env_count != 1 else ''}\n"
    )


@pytest.mark.parametrize(("output", "count"), [
    pytest.param("1 credential\n", 1, id="line-start-singular"),
    pytest.param("1 credentials\n", 1, id="line-start-plural"),
    pytest.param("3 credentials stored\n", 3, id="line-start-suffix"),
    pytest.param("Found 2 credentials\n", 2, id="word-prefix"),
    pytest.param("│ 4 credentials\n", 4, id="box-prefix"),
    pytest.param("(5 credentials)\n", 5, id="punctuation-prefix"),
])
def test_hf5_1_positive_decimal_total_reports_its_exact_count(tmp_path, output, count):
    result = check(tmp_path, output)
    assert (result.returncode, result.stdout) == (0, f"{count} stored credential(s)\n")


@pytest.mark.parametrize("count", [10, 20, 101])
@pytest.mark.parametrize("prefix", ["", "Found "])
def test_hf5_2_positive_total_is_never_a_zero_substring(tmp_path, count, prefix):
    result = check(tmp_path, f"{prefix}{count} credentials\n")
    assert (result.returncode, result.stdout) == (0, f"{count} stored credential(s)\n")


@pytest.mark.parametrize("output", [
    pytest.param("0 credentials\n", id="plain-zero"),
    pytest.param("Found 0 credential\n", id="prefixed-singular-zero"),
    pytest.param(sections("0 credentials", env_count=3), id="zero-with-env-keys"),
])
def test_hf5_2_exact_zero_reports_no_stored_credentials(tmp_path, output):
    result = check(tmp_path, output)
    assert result.returncode == 10
    assert "no stored credentials" in result.stdout


@pytest.mark.parametrize(("total", "count", "provider", "environment", "env_count"), [
    pytest.param("2 credentials", 2, "Example api", "Other OTHER_KEY", 10,
                 id="environment-count-is-not-stored-total"),
    pytest.param("2 credentials", 2, "7 credentials api", "Other OTHER_KEY", 1,
                 id="provider-name-is-not-stored-total"),
    pytest.param("2 credentials", 2, "0 credentials oauth", "Other OTHER_KEY", 1,
                 id="zero-in-provider-name"),
    pytest.param("2 credentials", 2, "Example api", "0 credentials EXAMPLE_KEY", 1,
                 id="zero-in-environment-row"),
])
def test_hf5_0_only_the_stored_total_counts(
        tmp_path, total, count, provider, environment, env_count):
    result = check(tmp_path, sections(total, provider=provider,
                                      environment=environment, env_count=env_count))
    assert (result.returncode, result.stdout) == (0, f"{count} stored credential(s)\n")


@pytest.mark.parametrize(("output", "count"), [
    pytest.param(sections("\x1b[32m3\x1b[0m \x1b[2mcredentials\x1b[0m"), 3,
                 id="coloured-footer"),
    pytest.param("\x1b[32m1\x1b[0m0 credentials\n", 10,
                 id="ansi-inside-decimal-count"),
])
def test_hf5_0_ansi_codes_and_decoration_are_ignored(tmp_path, output, count):
    result = check(tmp_path, output)
    assert (result.returncode, result.stdout) == (0, f"{count} stored credential(s)\n")


@pytest.mark.parametrize("output", [
    pytest.param("", id="empty"),
    pytest.param(" \n\t\n", id="whitespace"),
    pytest.param("Credentials: none\n", id="unrecognised-wording"),
    pytest.param(sections(None, provider="7 credentials api"), id="provider-row-only"),
    pytest.param("┌ Environment\n● Example KEY\n└ 3 environment variables\n",
                 id="environment-only"),
    pytest.param("┌ Environment\n● 2 credentials KEY\n└ 1 environment variable\n",
                 id="environment-credential-like-row-only"),
    pytest.param("Found 2 credentials\nFound 3 credentials\n", id="conflicting-positive-totals"),
    pytest.param("Found 0 credentials\nFound 2 credentials\n", id="zero-then-positive"),
    pytest.param("Found 2 credentials\nFound 0 credentials\n", id="positive-then-zero"),
    pytest.param("-1 credentials\n", id="negative-at-line-start"),
    pytest.param("└ -1 credentials\n", id="negative-decorated"),
    pytest.param("1.5 credentials\n", id="fraction-at-line-start"),
    pytest.param("└ 1.5 credentials\n", id="fraction-decorated"),
])
def test_hf5_3_absent_ambiguous_or_malformed_total_is_unknown(tmp_path, output):
    result = check(tmp_path, output)
    assert result.returncode == 20
    assert "unknown" in result.stdout.lower()
    assert "stored credential(s)" not in result.stdout


@pytest.mark.parametrize("output", ["", "0 credentials\n", "Found 2 credentials\n"])
def test_hf5_0_command_failure_is_unknown_even_with_a_total(tmp_path, output):
    result = check(tmp_path, output, status=1)
    assert result.returncode == 20
    assert "could not run" in result.stdout
    assert "stored credential(s)" not in result.stdout

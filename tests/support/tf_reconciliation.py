"""Fake-provider faults for contracts superseded by TF Decision 5."""
from pathlib import Path


def nonretryable_failure(provider):
    """An exited CLI with provider error text classifies as `other`, not crash.

    Keep the original early-exit/gate behaviour; only supply the missing error
    evidence. This edits the test's generated executable, never shared state.
    """
    script = Path(provider.entry["bin"])
    source = script.read_text()
    anchor = 'if fx.get("crash"):'
    assert anchor in source
    source = source.replace(anchor,
        'if fx.get("crash") or fx.get("exit", 0):\n'
        '    print("nonretryable fixture provider error", file=sys.stderr, flush=True)\n'
        + anchor, 1)
    script.write_text(source)

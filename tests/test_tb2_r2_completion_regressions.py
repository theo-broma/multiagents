"""TB2-R2: judge Codex repetitions only once their output is available."""
from test_tb2_r2_doom_loop_output import _codex_run


def test_result_change_at_the_threshold_is_not_a_loop(tmp_path, monkeypatch):
    assert _codex_run(tmp_path, monkeypatch, ["same", "same", "new"] * 4) == []


def test_completion_without_a_start_preserves_output(tmp_path, monkeypatch):
    calls = [{"type": "item.completed", "item": {
        "id": f"c{i}", "type": "command_execution", "command": "tail x.log",
        "status": "completed", "exit_code": 0, "aggregated_output": str(i),
    }} for i in range(12)]
    assert _codex_run(tmp_path, monkeypatch, calls) == []


def test_explicit_empty_output_still_detects_a_loop(tmp_path, monkeypatch):
    assert "doom_loop" in _codex_run(tmp_path, monkeypatch, [""] * 3)

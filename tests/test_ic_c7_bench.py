"""IC-C7 — `scripts/bench-isolation`, the latency benchmark (schema pinned by K11).

Flags: `--backend`, `--repetitions` (default 5), `--image cold|warm`,
`--seed-cache cold|warm`. Output, one JSON document on stdout:

    {"backend", "image", "seed_cache", "image_status", "samples": [
        {"loadavg": [..], "stages": {"seed", "provision", "start",
                                      "first_output", "exit", "cleanup"}}]}

Stage values are seconds, or the string "unsupported". On the reference
backend `seed` is "unsupported" and the top level has "image_status":
"unsupported". The reference backend is injected through the benchmark's own
`--backend reference` flag (never from project.yaml).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_support as ic  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bench-isolation"
STAGES = ["seed", "provision", "start", "first_output", "exit", "cleanup"]
TIMEOUT = 60


def bench(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(ic.SUPPORT), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
        IC_BACKEND_ROOT=str(tmp_path / "bench-root"))
    return subprocess.run([str(SCRIPT), "--backend", "reference", *args], capture_output=True,
                          text=True, timeout=TIMEOUT, env=env, cwd=tmp_path)


def doc_of(out):
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)                        # the whole of stdout is one document


def samples_of(doc):
    assert isinstance(doc.get("samples"), list), doc
    return doc["samples"]


def stage(sample, name):
    return sample["stages"][name]


def test_ic_c7_the_script_exists_and_is_executable():
    assert SCRIPT.is_file() and os.access(SCRIPT, os.X_OK)


def test_ic_c7_k11_the_document_schema_is_exact(tmp_path):
    doc = doc_of(bench(tmp_path, "--repetitions", "1"))
    assert set(doc) == {"backend", "image", "seed_cache", "image_status", "samples"}
    assert doc["backend"] == "reference"
    assert doc["image"] in ("cold", "warm") and doc["seed_cache"] in ("cold", "warm")
    (sample,) = samples_of(doc)
    assert set(sample) == {"loadavg", "stages"}
    assert set(sample["stages"]) == set(STAGES)
    load = sample["loadavg"]
    assert isinstance(load, list) and len(load) == 3
    assert all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in load)


def test_ic_c7_one_sample_per_repetition(tmp_path):
    assert len(samples_of(doc_of(bench(tmp_path, "--repetitions", "2")))) == 2


def test_ic_c7_repetitions_default_to_five(tmp_path):
    assert len(samples_of(doc_of(bench(tmp_path)))) == 5


def test_ic_c7_k11_reference_backend_seed_and_image_are_unsupported_never_zero(tmp_path):
    doc = doc_of(bench(tmp_path, "--repetitions", "1"))
    assert stage(samples_of(doc)[0], "seed") == "unsupported"
    assert doc["image_status"] == "unsupported"


def test_ic_c7_the_stages_the_reference_backend_performs_are_real_durations(tmp_path):
    sample = samples_of(doc_of(bench(tmp_path, "--repetitions", "1")))[0]
    for name in ("provision", "start", "first_output", "exit", "cleanup"):
        value = stage(sample, name)
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (name, value)
        assert value > 0, f"{name} reported as zero"


@pytest.mark.parametrize("image", ["cold", "warm"])
@pytest.mark.parametrize("cache", ["cold", "warm"])
def test_ic_c7_image_and_seed_cache_parameters_are_accepted_and_echoed(tmp_path, image, cache):
    doc = doc_of(bench(tmp_path, "--repetitions", "1", "--image", image, "--seed-cache", cache))
    assert doc["image"] == image and doc["seed_cache"] == cache
    assert doc["image_status"] == "unsupported"


@pytest.mark.parametrize("bad", [["--repetitions", "0"], ["--repetitions", "-1"],
                                 ["--repetitions", "x"], ["--image", "tepid"], ["--seed-cache", "x"],
                                 ["--backend", "firecracker"]])
def test_ic_c7_bad_parameters_fail_without_a_document(tmp_path, bad):
    out = bench(tmp_path, *bad)
    assert out.returncode != 0
    assert out.stdout.strip() == ""

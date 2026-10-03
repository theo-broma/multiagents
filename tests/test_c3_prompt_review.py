"""PF-R1/PF-R2/PF-R3: native framing, mapped mounts and trusted roots."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from multiagents import agentwrap
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths, global_config_dir
from multiagents.providers import load_providers

sys.path.insert(0, str(Path(__file__).parent / "support"))
import pf_harness as pf
import sp_harness as sp

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["wrapper", "adapter"])
def reader(request):
    if request.param == "wrapper":
        return agentwrap.read_prompt
    source = ROOT / "src/multiagents/defaults/providers/codex.py"
    spec = importlib.util.spec_from_file_location("pf_codex_reader", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_prompt


def test_workspace_symlink_is_allowed_above_the_run_directory(tmp_path, reader):
    workspace = tmp_path / "workspace"
    run_dir = workspace / "runs" / "turn"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_bytes(b"  literal \xc3\xa9\n\n")
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    aliased_run = alias / "runs" / "turn"
    assert reader(str(aliased_run / "prompt.md"), 4096, str(aliased_run)) == b"  literal \xc3\xa9\n\n"


@pytest.mark.parametrize("component", ["file", "directory"])
def test_links_inside_the_run_directory_are_still_refused(tmp_path, reader, component):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.md").write_bytes(b"victim")
    if component == "file":
        path = run_dir / "prompt.md"
        path.symlink_to(outside / "prompt.md")
    else:
        (run_dir / "subdir").symlink_to(outside, target_is_directory=True)
        path = run_dir / "subdir" / "prompt.md"
    with pytest.raises(OSError):
        reader(str(path), 4096, str(run_dir))


def test_read_cannot_escape_the_run_directory(tmp_path, reader):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "prompt.md"
    outside.write_bytes(b"victim")
    with pytest.raises(ValueError, match="run directory"):
        reader(str(outside), 4096, str(run_dir))


@pytest.mark.parametrize("transport", ["stdin", "file"])
def test_wrapped_docker_input_uses_the_container_bind_destination(tmp_path, monkeypatch, transport):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    run_dir = paths.run_dir("pf-mapped")
    run_dir.mkdir(parents=True)
    prompt = run_dir / "prompt.md"
    data = b"  mapped \xc3\xa9 \xf0\x9f\x98\x80\n\n"
    prompt.write_bytes(data)
    prompt.chmod(0o600)
    container_store = tmp_path / "container-store"
    container_store.symlink_to(paths.data, target_is_directory=True)
    sp.install_fake_docker(tmp_path, monkeypatch)
    ex = DockerExecutor({"network": "bridge", "mount_cli_from_host": False},
                        paths, {}, global_config_dir())
    monkeypatch.setattr(ex, "inside", lambda: False)
    monkeypatch.setattr(ex, "ensure_running", lambda: {"ok": True})
    monkeypatch.setattr(ex, "backing", lambda: {container_store: paths.data})
    capture = tmp_path / "captured"
    code = ("import pathlib,sys; "
            "data=pathlib.Path(sys.argv[2]).read_bytes() if len(sys.argv)>2 "
            "else sys.stdin.buffer.read(); pathlib.Path(sys.argv[1]).write_bytes(data)")
    argv = [sys.executable, "-c", code, str(capture)]
    if transport == "file":
        argv.append(str(prompt))
    env = {"MULTIAGENTS_AGENT_ID": "pf-mapped", "PATH": os.environ["PATH"],
           "MULTIAGENTS_PROMPT_FILE": str(prompt),
           "MULTIAGENTS_PROMPT_RUN_DIR": str(run_dir),
           "MULTIAGENTS_PROMPT_TRANSPORT": transport}

    async def launch():
        handle = await ex.start(argv, paths.root, env, run_dir=run_dir)
        return await asyncio.to_thread(handle._proc.wait, timeout=15)

    assert asyncio.run(launch()) == 0
    assert capture.read_bytes() == data
    written = dict(line.split("=", 1) for line in ex.env_file("pf-mapped").read_text().splitlines())
    assert written["MULTIAGENTS_PROMPT_FILE"] == str(container_store / "runs/pf-mapped/prompt.md")
    assert written["MULTIAGENTS_PROMPT_RUN_DIR"] == str(container_store / "runs/pf-mapped")


def test_unwrapped_docker_snapshots_stdin_from_the_host_path(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    run_dir = paths.run_dir("pf-direct")
    run_dir.mkdir(parents=True)
    prompt = run_dir / "prompt.md"
    prompt.write_bytes(b"host-only input\n\n")
    sp.install_fake_docker(tmp_path, monkeypatch)
    ex = DockerExecutor({"network": "bridge", "mount_cli_from_host": False},
                        paths, {}, global_config_dir())
    monkeypatch.setattr(ex, "inside", lambda: False)
    monkeypatch.setattr(ex, "ensure_running", lambda: {"ok": True})
    # This destination need not exist on the host: only native stdin crosses
    # docker exec in this path, so the host must snapshot the source first.
    monkeypatch.setattr(ex, "backing", lambda: {Path("/pf/container-only"): paths.data})
    capture = tmp_path / "captured"
    argv = [sys.executable, "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
            str(capture)]
    env = {"MULTIAGENTS_AGENT_ID": "pf-direct", "PATH": os.environ["PATH"],
           "MULTIAGENTS_PROMPT_FILE": str(prompt),
           "MULTIAGENTS_PROMPT_RUN_DIR": str(run_dir),
           "MULTIAGENTS_PROMPT_TRANSPORT": "stdin"}

    async def launch():
        handle = await ex.start(argv, paths.root, env)
        return await asyncio.wait_for(handle._proc.wait(), 15)

    assert asyncio.run(launch()) == 0
    assert capture.read_bytes() == b"host-only input\n\n"


def test_container_path_selects_the_deepest_source_mount(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path / "project")
    ex = DockerExecutor({}, paths, {}, global_config_dir())
    monkeypatch.setattr(ex, "inside", lambda: False)
    monkeypatch.setattr(ex, "backing", lambda: {
        Path("/container/project"): paths.root,
        Path("/container/data"): paths.data,
    })
    assert ex.container_path(paths.run_dir("turn") / "prompt.md") == Path(
        "/container/data/runs/turn/prompt.md")


def test_strict_agy_flag_parser_and_event_envelope_on_the_real_launch_path(tmp_path, monkeypatch):
    rig = pf.Rig(tmp_path, monkeypatch, names=("agy",))
    native = rig.natives["agy"]
    native.path.write_text(f"#!{sys.executable}\n" + '''
import json, sys
args = sys.argv[1:]
i = args.index('-p')
if i + 1 == len(args) or args[i + 1].startswith('-'):
    raise SystemExit('print flag consumed another flag as its prompt')
if args[i + 1] != '':
    raise SystemExit('print argument should not supply another message')
if args[args.index('--input-format') + 1] != 'stream-json':
    raise SystemExit('stream-json input is required')
for line in sys.stdin:
    record = json.loads(line)
    if record.get('event') != 'user':
        raise SystemExit('stream input message is missing the event field')
    if record['message']['role'] != 'user':
        raise SystemExit('expected a user message')
    from pathlib import Path
    Path(__file__).with_name('received').write_bytes(record['message']['content'].encode())
print(json.dumps({'event':'result','result':{'status':'success','response':'ok'}}))
''')
    result = rig.start("agy", "  literal \u00e9 \U0001f600\n\n")
    node = rig.node(result["agent_id"])
    assert node.status == "done", (result, node.reason)
    assert native.path.with_name("received").read_bytes() == rig.prompt_md(node.id)

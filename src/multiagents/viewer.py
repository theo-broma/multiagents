import sys
import json
import os
import re
import textwrap
import time
from pathlib import Path

from .paths import ProjectPaths
from .tree import ACTIVE, TERMINAL, Tree

# Bidi controls can reorder what a terminal shows, so they are escaped like
# control bytes (TM-R1a).
_BIDI = frozenset([0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F),
                   *range(0x2066, 0x206A)])
_FINGERPRINT = 64
# How long a view keeps following after its run turned terminal (TM-R3). A
# module constant so a test can shorten it; nothing in production sets it.
LINGER_SECONDS = 60

def _validate_id(agent_id: str):
    if not re.match(r"^ag-[0-9a-f]{6}(-[0-9]+)?\Z", agent_id):
        print(f"invalid agent id: {agent_id}", file=sys.stderr)
        sys.exit(2)

def format_text(text: str) -> str:
    # C0 and C1 control characters, ESC, CR and backspace are escaped visibly;
    # newline and tab are kept;
    # no stream content is ever passed to a shell or used as a tmux target.
    res = []
    for char in text:
        code = ord(char)
        if code == 0x0A or code == 0x09:
            res.append(char)
        elif code < 0x20 or code == 0x7F or (0x80 <= code <= 0x9F):
            res.append(f"\\x{code:02x}")
        elif 0xD800 <= code <= 0xDFFF or code in _BIDI:
            res.append(f"\\u{code:04x}")
        else:
            res.append(char)
    return "".join(res)

def _status(node) -> str:
    status = node.get("status") if isinstance(node, dict) else getattr(node, "status", None)
    return str(status).lower() if status else ""


def _is_terminal(node) -> bool:
    return _status(node) in TERMINAL


def _emit(out: str) -> None:
    # Nothing that reaches stdout may raise an encoding error (TM-R1a).
    try:
        print(out, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(out.encode(enc, "backslashreplace").decode(enc), flush=True)


def _same_file(stream: Path, offset: int, head: bytes, tail: bytes) -> bool:
    """Do the bytes already consumed still sit where we read them (TM-R1b)?

    A file that was truncated and then grew past the old offset is not shorter
    and may keep its inode, so compare a fingerprint of what was consumed.
    """
    try:
        with open(stream, "rb") as chk:
            if chk.read(len(head)) != head:
                return False
            chk.seek(offset - len(tail))
            return chk.read(len(tail)) == tail
    except OSError:
        return False


def view_stream(paths: ProjectPaths, agent_id: str, follow: bool | None):
    _validate_id(agent_id)
    run_dir = paths.run_dir(agent_id)
    stream = run_dir / "stream.jsonl"
    if stream.is_symlink() and not stream.exists():
        print(f"refused dangling symlink: {stream}", file=sys.stderr)
        sys.exit(2)

    # an unknown agent id exits 2
    tree = Tree(paths.tree_file, paths.events_file)
    node = tree.get(agent_id)
    if not node and not run_dir.exists():
        print(f"unknown agent: {agent_id}", file=sys.stderr)
        sys.exit(2)

    if follow is None:
        follow = _status(node) in ACTIVE

    # symlink or FIFO is refused
    if stream.exists():
        if stream.is_symlink() or not stream.is_file():
            print("stream is not a regular file", file=sys.stderr)
            sys.exit(1)

    fh = None
    last_ino = None
    offset = 0          # bytes consumed from the current file
    head = b""          # first bytes consumed, the fingerprint of the file
    tail = b""          # last bytes consumed

    # The view exits within 90 s after the run turns terminal; it lingers
    # LINGER_SECONDS so that the window stays readable (TM-R3).
    terminal_time = None

    while True:
        if not stream.exists():
            if not follow:
                sys.exit(0)
            if _is_terminal(tree.get(agent_id)):
                break
            time.sleep(0.5)
            continue

        try:
            st = stream.stat()
        except OSError:
            time.sleep(0.5)
            continue

        if (fh is None or st.st_ino != last_ino or st.st_size < offset
                or (offset and not _same_file(stream, offset, head, tail))):
            if fh is not None:
                _emit("--- stream truncated ---")
                fh.close()
            try:
                fh = open(stream, "rb")
            except OSError:
                fh = None
                time.sleep(0.5)
                continue
            last_ino = st.st_ino
            offset = 0
            head = tail = b""

        while True:
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                # a line still being written is not printed half-read
                fh.seek(offset)
                break
            offset += len(raw)
            head = (head + raw)[:_FINGERPRINT] if len(head) < _FINGERPRINT else head
            tail = (tail + raw)[-_FINGERPRINT:]
            line = raw.decode("utf-8", errors="replace")

            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            kind = event.get("kind") or event.get("type") or "raw"
            text = event.get("text") or event.get("content") or ""
            if not isinstance(text, str):
                text = json.dumps(text)[:2000]

            tool = event.get("tool") or event.get("name") or ""
            args = event.get("arguments") or event.get("args") or ""

            if kind == "text":
                text_fmt = format_text(text)
                out = "\n".join(textwrap.fill(ln, width=120, expand_tabs=False, replace_whitespace=False, drop_whitespace=False) for ln in text_fmt.splitlines())
            elif kind == "tool":
                out = f"tool {tool}: {json.dumps(args)[:200]}..." if len(json.dumps(args)) > 200 else f"tool {tool}: {json.dumps(args)}"
            elif kind == "result":
                out = f"result: {text}"
            elif kind == "error":
                out = f"error: {text}"
            else:
                out = text

            out = format_text(out)
            out = out[:4000]
            if len(out) == 4000:
                out += " [truncated]"

            _emit(out)

        if not follow:
            break

        node = tree.get(agent_id)
        if _is_terminal(node):
            if terminal_time is None:
                terminal_time = time.time()
                _emit(f"final status: {_status(node)}")
            if time.time() - terminal_time > LINGER_SECONDS:
                break
        time.sleep(0.2)

    if not follow:
        node = tree.get(agent_id)
        if _is_terminal(node):
            _emit(f"final status: {_status(node)}")

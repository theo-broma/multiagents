#!/usr/bin/env python3
"""Refresh the packaged PSL deliberately; commit the snapshot separately."""

from pathlib import Path
import argparse
import re
from urllib.request import urlopen


SECTION_MARKERS = {
    f"// ==={boundary} {section} DOMAINS===".encode()
    for boundary in ("BEGIN", "END")
    for section in ("ICANN", "PRIVATE")
}


def filter_snapshot(snapshot: bytes) -> bytes:
    """Keep the upstream license and rules, without contributor comments."""
    lines = snapshot.splitlines()
    license_start = next(
        index for index, line in enumerate(lines)
        if line.startswith(b"// This Source Code Form is subject to")
    )
    license_end = license_start
    while license_end < len(lines) and lines[license_end].startswith(b"//"):
        license_end += 1
        if b"https://mozilla.org/MPL/2.0/" in lines[license_end - 1]:
            break
    license_lines = lines[license_start:license_end]
    if not any(b"https://mozilla.org/MPL/2.0/" in line for line in license_lines):
        raise ValueError("missing Public Suffix List license")
    if not SECTION_MARKERS.issubset(lines):
        raise ValueError("incomplete Public Suffix List")
    retained = license_lines + [
        line for line in lines
        if line in SECTION_MARKERS or (line.strip() and not line.startswith(b"//"))
    ]
    return b"\n".join(retained) + b"\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="?", help="full commit id from publicsuffix/list")
    parser.add_argument("--from-file", type=Path, help="filter a local snapshot without downloading")
    args = parser.parse_args()
    if args.from_file is not None:
        if args.revision is not None:
            parser.error("revision and --from-file are mutually exclusive")
        snapshot = args.from_file.read_bytes()
    else:
        if args.revision is None or not re.fullmatch(r"[0-9a-f]{40}", args.revision):
            parser.error("revision must be a full upstream commit id")
        url = f"https://raw.githubusercontent.com/publicsuffix/list/{args.revision}/public_suffix_list.dat"
        with urlopen(url, timeout=60) as response:
            snapshot = response.read()
    target = Path(__file__).resolve().parents[1] / "src/multiagents/public_suffix_list.dat"
    target.write_bytes(filter_snapshot(snapshot))

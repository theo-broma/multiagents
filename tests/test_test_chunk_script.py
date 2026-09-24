import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "test-chunk.sh"


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_chunk_partition_and_coverage(n: int) -> None:
    expected_files = set(str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.glob("tests/test_*.py"))
    assert len(expected_files) > 0

    chunks = []
    for k in range(1, n + 1):
        proc = subprocess.run(
            [str(SCRIPT_PATH), "--list", str(k), str(n)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        chunk_files = set(line.strip() for line in proc.stdout.splitlines() if line.strip())
        chunks.append(chunk_files)

    # Verify no two chunks overlap
    for i in range(len(chunks)):
        for j in range(i + 1, len(chunks)):
            assert chunks[i].isdisjoint(chunks[j]), f"Chunk {i + 1} and {j + 1} overlap for N={n}"

    # Verify the union of all chunks equals the set of tests/test_*.py
    union_files = set().union(*chunks)
    assert union_files == expected_files

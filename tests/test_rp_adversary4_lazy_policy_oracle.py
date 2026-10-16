"""Adversary pass 4 on readonly restore by provenance: RP13's lazy policy reads.

A seeded property test drives `Results.readonly_decision` directly on random
commit graphs (merges, criss-cross bases, deletions, mode changes, commits on
the base first-parent chain that are also recorded generations) and compares
it with an oracle written from the spec text (RP7 steps 1-4, RP10, RP11, RB-R1),
independent of the implementation.

Every generation whose producer policy is unreadable (missing record, no
readonly_paths, malformed) is given each possible real policy in turn. RP13:
a policy that was not read must not steer anything, so whenever capture
decides, its decision must equal the oracle's under every assignment. When
no policy is unreadable, the decision must equal the oracle's exactly.
"""
from __future__ import annotations

import itertools
import os
import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents import gitops
from multiagents.config import matches_any
from multiagents.scheduler.results import Results

PROTECTED = ["tests/a.py", "tests/b.py", "tests/c.py"]
PATTERNS = ["tests/**"]
VALUES = [None, ("100644", "v0"), ("100644", "v1"), ("100644", "v2"), ("100755", "v1")]
READABLE = [[[]], [["tests/**"]], [["tests/a.py"]], [["**", "!tests/b.py"]], [[], ["tests/c.py"]]]
UNREADABLE = [[None], [["tests/**", 5]], ["tests/**"], [[], None]]
ASSIGN = [[[]], [["tests/**"]], [["tests/a.py"]]]


class Repo:
    def __init__(self, root):
        self.root = root
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"}
        self.git("init", "-q", "-b", "main")
        self.blobs, self.count = {}, 0

    def git(self, *args, input=None, env=None):
        return subprocess.run(["git", *args], cwd=self.root, input=input, text=True, check=True,
                              capture_output=True, env={**self.env, **(env or {})}).stdout.strip()

    def blob(self, text):
        if text not in self.blobs:
            self.blobs[text] = self.git("hash-object", "-w", "--stdin", input=text + "\n")
        return self.blobs[text]

    def commit(self, state, parents):
        index = str(self.root / ".git" / "scratch-index")
        if os.path.exists(index):
            os.unlink(index)
        lines = "".join(f"{mode} {self.blob(text)}\t{path}\n" for path, (mode, text) in state.items() if text)
        self.git("update-index", "--add", "--index-info", input=lines, env={"GIT_INDEX_FILE": index})
        tree = self.git("write-tree", env={"GIT_INDEX_FILE": index})
        self.count += 1
        args = [x for parent in parents for x in ("-p", parent)]
        return self.git("commit-tree", tree, *args, "-m", f"c{self.count}")


def mutate(rng, state, p=0.4):
    state = dict(state)
    for path in PROTECTED:
        if rng.random() < p:
            value = rng.choice(VALUES)
            if value is None:
                state.pop(path, None)
            else:
                state[path] = value
    if rng.random() < 0.5:
        state["src/m.py"] = ("100644", "s%d" % rng.randrange(1000))
    return state


def build(rng, root):
    repo = Repo(root)
    states = {}
    base = {"src/m.py": ("100644", "s"), **{p: ("100644", "v0") for p in PROTECTED}}
    r = repo.commit(base, [])
    states[r] = base
    pool = [r]
    for _ in range(rng.randrange(0, 3)):
        st = mutate(rng, base)
        c = repo.commit(st, [r])
        states[c] = st
        pool.append(c)
    parents = [r] if len(pool) == 1 or rng.random() < 0.5 else rng.sample(pool, 2)
    istate = mutate(rng, states[parents[0]])
    inp = repo.commit(istate, parents)
    states[inp] = istate
    pool.append(inp)
    for _ in range(rng.randrange(3, 9)):
        parents = rng.sample(pool, 2) if len(pool) > 1 and rng.random() < 0.35 else [rng.choice(pool)]
        st = mutate(rng, states[parents[0]] if rng.random() < 0.7 else states[parents[-1]])
        c = repo.commit(st, parents)
        states[c] = st
        pool.append(c)
    main = rng.choice(pool)
    repo.git("update-ref", "refs/heads/main", main)
    merged = rng.sample(pool, rng.randrange(0, min(4, len(pool))))
    tparents = [inp] + [c for c in merged if c != inp]
    src_state = states[rng.choice(tparents)]
    tstate = mutate(rng, src_state, 0.5) if rng.random() < 0.8 else dict(src_state)
    tip = repo.commit(tstate, tparents)
    if rng.random() < 0.3:
        tip = repo.commit(mutate(rng, tstate, 0.3), [tip])
    gens = {}
    for c in pool:
        if c != r and rng.random() < 0.55:
            kind = rng.random()
            gens[c] = {"ag-%06x" % rng.randrange(1 << 24):
                       rng.choice(UNREADABLE) if kind < 0.35 else rng.choice(READABLE)}
    return repo, inp, tip, gens


def readable(policies):
    return bool(policies) and not any(not records or any(
        not isinstance(policy, list) or any(not isinstance(x, str) for x in policy) for policy in records)
        for records in policies.values())


def oracle(repo, inp, tip, paths, gens, base_chain):
    """RP7/RP10/RP11 from the spec text, with every policy readable."""
    # RB-R1: newest-first frozen base chain; matching anchors are per path.
    chain = repo.git("rev-list", "--first-parent", "main").split()
    history = repo.git("rev-list", "--topo-order", tip, "--not", inp).split()
    candidates = [c for c in history if c in base_chain or c in gens]

    def entry(commit, path):
        out = repo.git("ls-tree", commit, "--", path)
        return out.split("\t")[0].split(" ") if out else None

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=repo.root).returncode == 0

    def bases(c):
        r = subprocess.run(["git", "merge-base", "--all", c, inp], cwd=repo.root, capture_output=True, text=True)
        return r.stdout.split()

    out = []
    for path in paths:
        allowed = [c for c in candidates if c in base_chain or all(
            not matches_any(policy, path) for records in gens[c].values() for policy in records)]
        remaining = [c for c in allowed if not any(d != c and is_ancestor(c, d) for d in allowed)]
        anchor = next((i for i, c in enumerate(chain)
                       if entry(c, path) == entry(inp, path)), None)
        newer_base = set(chain[:anchor]) if anchor is not None else set()
        # RP7 supersession still applies; only merged candidates qualify.
        vouching = [c for c in remaining if c in newer_base or (bases(c) and all(
            entry(c, path) != entry(b, path) and entry(inp, path) == entry(b, path) for b in bases(c)))]
        t = entry(tip, path)
        matches = [c for c in vouching if entry(c, path) == t]
        if matches:
            source = next((c for c in matches if c in base_chain), matches[0])
            out.append({"path": path, "outcome": "kept", "source": source})
            continue
        alternatives = {tuple(entry(c, path) or ()) for c in vouching}
        out.append({"path": path, "outcome": "reverted",
                    "source": vouching[0] if len(alternatives) == 1 else inp})
    return out


SEEDS = range(int(os.environ.get("RP_ADV4_SEED0", "0")), int(os.environ.get("RP_ADV4_SEEDS", "120")))


@pytest.mark.parametrize("seed", SEEDS)
def test_rp13_an_unread_policy_never_steers_the_decision(tmp_path, seed):
    rng = random.Random(seed)
    repo, inp, tip, gens = build(rng, tmp_path)
    changed = repo.git("diff", "--no-renames", "--name-only", "--diff-filter=MDT", inp, tip).split("\n")
    paths = [p for p in changed if p and matches_any(PATTERNS, p)]
    if not paths:
        pytest.skip("no protected path changed")
    results = Results(SimpleNamespace(root=tmp_path, scheduler=tmp_path), SimpleNamespace(base_branch="main"),
                      generation_commits=set(gens), generation_policies=gens)
    base_chain = set(repo.git("rev-list", "--first-parent", "main").split())
    history = set(repo.git("rev-list", tip, "--not", inp).split())
    unread = [c for c in gens if c in history and c not in base_chain and not readable(gens[c])]
    try:
        decision = results.readonly_decision({"input_commit": inp}, tip, paths)
    except gitops.GitError as exc:
        assert "producer policy is unavailable" in str(exc), exc
        assert unread, "capture failed with every policy readable"
        return
    for c, policies in decision["producer_policies"].items():
        assert c not in base_chain and readable(policies) and policies == gens[c]
    for choice in itertools.product(ASSIGN, repeat=len(unread)):
        assigned = {**gens, **{c: {"ag-z": p} for c, p in zip(unread, choice)}}
        assert decision["paths"] == oracle(repo, inp, tip, paths, assigned, base_chain), (
            f"seed {seed}: unread {unread} assigned {choice}")

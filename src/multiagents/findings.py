"""The findings ledger: what a review found, and what became of it.

Two kinds of file, and the distinction is the whole design.

A **findings file** — ``context/review/<context>.md`` — is what an auditor wrote
while looking at the code. It is historical evidence, tied to the commit it was
written against, and nothing ever edits it. Marking ``F12`` "fixed" inside it
would leave the line numbers and the trace pointing at code that has since moved,
which is how an audit trail becomes a set of confident lies.

The **ledger** — ``context/review/ledger.yaml`` — is the state machine over those
ids. Append-only history, one current status per finding. When the two disagree
the ledger wins, because the finding is a record of a moment and the ledger is a
record of decisions.

It is also what stops the loop. review → fix → review → fix generates work
forever if each pass starts from nothing: the second review of a context the
implement team has touched will map it afresh and file the same problems under
new ids. Against the ledger it cannot. A finding that was claimed fixed and is
still there is a **regression on F12**, not a new F105, and a project is done
being reviewed when the ledger has no open or regressed findings anyone wants to
spend budget on. That turns the second review from another exploration into a
verification, which is a job that terminates.

Written only by the orchestrator and the initializer, both of which run at the
project root. Agents write findings files in their own worktrees and never touch
this, so several of them merging cannot conflict over it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LEDGER = Path("context/review/ledger.yaml")
FINDINGS_DIR = Path("context/review")

# `**F12** — a one-sentence summary`, then `*Field:* value` lines until the next
# finding or heading. Deliberately forgiving about everything except the id: a
# findings file is written by a model, and refusing to record a finding because
# it omitted `*Reasoning:*` would lose the finding to punish the formatting.
_HEAD = re.compile(r"^\*\*(F\d+)\*\*\s*[—-]\s*(.*)$", re.M)
_FIELD = re.compile(r"^\*([A-Za-z ]+):\*\s*(.*)$", re.M)

OPEN = "open"
SCHEDULED = "scheduled"
FIXED = "fixed"
ACCEPTED = "accepted"
DEFERRED = "deferred"
REGRESSION = "regression"

# What counts as still needing someone's attention. `accepted` and `deferred`
# are decisions, not unfinished business — a project with fifty accepted
# findings is a project whose owner has looked at fifty things and said no.
LIVE = {OPEN, SCHEDULED, REGRESSION}

STATUSES = {OPEN, SCHEDULED, FIXED, ACCEPTED, DEFERRED, REGRESSION}


@dataclass
class Finding:
    id: str
    summary: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return (self.fields.get("Severity") or "").strip().lower()

    @property
    def finding_class(self) -> str:
        return (self.fields.get("Class") or "").strip().lower()

    @property
    def evidence(self) -> str:
        return (self.fields.get("Evidence") or "").strip().lower()


def parse(text: str) -> list[Finding]:
    """Every finding in one findings file, in the order written."""
    out: list[Finding] = []
    heads = list(_HEAD.finditer(text))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[head.end():end]
        fields = {name.strip(): value.strip()
                  for name, value in _FIELD.findall(body)}
        out.append(Finding(id=head.group(1), summary=head.group(2).strip(),
                           fields=fields))
    return out


def parse_file(path: Path) -> list[Finding]:
    return parse(path.read_text()) if path.is_file() else []


def load(root: Path) -> dict[str, Any]:
    path = root / LEDGER
    if not path.is_file():
        return {"findings": {}}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("findings", {})
    return data


def save(root: Path, data: dict[str, Any]) -> Path:
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))
    return path


def _entry(finding: Finding, source: str, sha: str) -> dict[str, Any]:
    return {
        "context": source,
        "summary": finding.summary[:300],
        "severity": finding.severity,
        "class": finding.finding_class,
        "evidence": finding.evidence,
        "first_seen": sha,
        "status": OPEN,
        "history": [{"status": OPEN, "by": "auditor", "sha": sha,
                     "note": f"filed in {source}"}],
    }


def record(root: Path, source: str, sha: str = "") -> dict[str, Any]:
    """Ingest a findings file into the ledger. Mechanical, not a judgement.

    New ids are added as `open`. An id already in the ledger is NOT overwritten
    — it carries decisions someone made, and a re-run of the auditor must not
    quietly reopen a finding the user accepted. What it does instead is the
    interesting case: an id that was `fixed` and has been filed again is a
    **regression**, which is the signal that the loop is not converging.
    """
    path = root / source
    findings = parse_file(path)
    data = load(root)
    ledger = data["findings"]

    added, regressed, unchanged = [], [], []
    for finding in findings:
        existing = ledger.get(finding.id)
        if existing is None:
            ledger[finding.id] = _entry(finding, source, sha)
            added.append(finding.id)
            continue
        if existing.get("status") == FIXED:
            existing["status"] = REGRESSION
            existing.setdefault("history", []).append(
                {"status": REGRESSION, "by": "auditor", "sha": sha,
                 "note": "filed again after being marked fixed"})
            regressed.append(finding.id)
        else:
            unchanged.append(finding.id)

    save(root, data)
    return {"source": source, "parsed": len(findings), "added": added,
            "regressions": regressed, "already_known": unchanged}


def set_status(root: Path, finding_id: str, status: str, note: str = "",
               sha: str = "", by: str = "orchestrator") -> dict[str, Any]:
    """Move one finding along, appending to its history rather than replacing it."""
    if status not in STATUSES:
        return {"error": f"unknown status {status!r}; use one of "
                         f"{', '.join(sorted(STATUSES))}"}
    data = load(root)
    entry = data["findings"].get(finding_id)
    if entry is None:
        return {"error": f"{finding_id} is not in the ledger. Record its findings "
                         f"file first, or check the id."}
    was = entry.get("status", OPEN)
    entry["status"] = status
    entry.setdefault("history", []).append(
        {"status": status, "by": by, "sha": sha, "note": note})
    save(root, data)
    return {"id": finding_id, "was": was, "status": status}


def summary(root: Path, status: str = "", context: str = "") -> dict[str, Any]:
    """The ledger as an index: ids, status, severity. Never the evidence.

    Deliberately excludes the trace, the proof and the reasoning. Whoever is
    reading this is deciding what to work on, and a reader that loads every
    finding's evidence to make that decision has spent its context before it
    reaches the decision.
    """
    entries = load(root)["findings"]
    rows = []
    for fid, entry in sorted(entries.items(), key=lambda kv: _idnum(kv[0])):
        if status and entry.get("status") != status:
            continue
        if context and entry.get("context") != context:
            continue
        rows.append({"id": fid, "status": entry.get("status", OPEN),
                     "severity": entry.get("severity", ""),
                     "class": entry.get("class", ""),
                     "evidence": entry.get("evidence", ""),
                     "context": entry.get("context", ""),
                     "summary": entry.get("summary", "")})
    counts: dict[str, int] = {}
    for entry in entries.values():
        counts[entry.get("status", OPEN)] = counts.get(entry.get("status", OPEN), 0) + 1
    live = sum(counts.get(s, 0) for s in LIVE)
    return {"findings": rows, "counts_by_status": counts, "live": live,
            "done": live == 0}


def _idnum(fid: str) -> int:
    try:
        return int(fid[1:])
    except (ValueError, IndexError):
        return 0


def evidence(root: Path, finding_id: str) -> dict[str, Any]:
    """One finding's full text, pulled on demand.

    The reason this exists rather than "read the file": a findings file holds
    every finding for a context, and an agent that opens it to answer a question
    about one of them has loaded all of them. This is the lazy read that keeps a
    conversation about `F12` costing the size of `F12`.
    """
    data = load(root)
    entry = data["findings"].get(finding_id)
    source = entry.get("context") if entry else ""
    candidates = [root / source] if source else sorted((root / FINDINGS_DIR).glob("*.md"))
    for path in candidates:
        for finding in parse_file(path):
            if finding.id == finding_id:
                return {"id": finding_id, "source": str(path.relative_to(root)),
                        "summary": finding.summary, "fields": finding.fields,
                        "status": (entry or {}).get("status", "not in ledger"),
                        "history": (entry or {}).get("history", [])}
    return {"error": f"{finding_id} not found in any findings file under "
                     f"{FINDINGS_DIR}/"}

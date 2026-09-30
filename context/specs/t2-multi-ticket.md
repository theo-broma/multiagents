# T2: one bug-reporter run can file several tickets, the contract

**Status:** contract, written by the orchestrator on 2026-09-30.
- **Source:**
  - Run ag-3afd5a ended with three `TICKET(...)` markers, and only the last one was filed (bug-ac396a).
  - The current rule is "the last marker wins" (`runner._file_ticket`). It exists so that a model quoting the literal marker while reasoning does not file its monologue as a ticket.
- **Ids:** `MT-R*`. They are never renumbered.

## Behaviours

**MT-R1: every real marker files a ticket.**
- **What a real marker is.** It is a line that starts, after optional spaces, with `TICKET(blocking|minor):`. That is the existing regex.
- **What each marker files.** Each one files one ticket:
  - its title is the rest of the line;
  - its body is the text up to the next real marker, or to the end;
  - its proposed fix is the part of that body after `PROPOSED_FIX`, when present. The existing split is applied per section.
- Verified by: a message with three markers files three tickets. Each has the right title, severity, body and fix, and no body contains another ticket's text.

**MT-R2: quoted markers are not markers.**
- **What is ignored.** A marker is ignored when it sits:
  - inside a fenced code block (```` ``` ````);
  - on a line that starts with `>`;
  - inside inline backticks.
- **What survives.** A monologue that quotes the rule, followed by one real ticket at the end, files exactly that one ticket.
- Verified by:
  - the monologue case;
  - a marker inside a fenced block;
  - a marker inside a quoted line.

**MT-R3: an empty section is not a ticket.**
- **What is dropped.** A marker whose section body is empty or whitespace-only, and which has no fix, files nothing.
- **Duplicates.** Two sections with an identical title file one ticket, and it is the later section that is kept.
- Verified by:
  - an empty section between two real ones files two tickets;
  - a duplicated title files one ticket.

**MT-R4: where the tickets are reported.**
- **Every filed ticket is reported.** The node and the result record every filed ticket. The field that held one ticket, `ticket`, becomes a list `tickets`, and `ticket` stays as the last one for compatibility.
- **Visible in the collection.** `collect_agent` shows every filed ticket.
- Verified by: a three-ticket run exposes three ids.

**MT-R5: nothing else changes.**
- A single-marker message behaves exactly as today.
- The existing ticket tests stay green.

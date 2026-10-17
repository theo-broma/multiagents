"""The fake host-side credential refresher for slice 4C (D-S4C.4).

Loaded by the SCHEDULER process through the assumed seam
`MULTIAGENTS_TEST_CREDENTIAL_REFRESH=module:factory` (NEED_INFO(s4c-seams)).
`factory(root, *, project)` returns an object with

    refresh(provider, account, entry) -> dict      # the account's NEW vault entry

Every call is appended to the file named by IC_S4C_REFRESH_LOG, one JSON line
for entry and one for exit, so a test in another process can read the overlap
and the count of refreshes. IC_S4C_REFRESH_DELAY holds each call open for that
many seconds (a concurrent second refresh would overlap it). All values are
fictional.
"""
from __future__ import annotations

import json
import os
import time
import uuid

LOG_ENV = "IC_S4C_REFRESH_LOG"
DELAY_ENV = "IC_S4C_REFRESH_DELAY"
FAR_FUTURE_MS = 4102444800000          # 2100-01-01
SPEC = "ic_s4c_refresh:factory"


def _log(event: str, **fields) -> None:
    path = os.environ.get(LOG_ENV)
    if path:
        line = json.dumps({"event": event, "pid": os.getpid(), "t": time.monotonic(), **fields}) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)


class Refresher:
    def refresh(self, provider, account, entry):
        _log("enter", provider=provider, account=account)
        time.sleep(float(os.environ.get(DELAY_ENV, "0") or 0))
        tag = uuid.uuid4().hex[:12]
        fresh = dict(entry)
        if entry.get("type") == "api":
            fresh["key"] = "FICTION-RENEWED-KEY-" + tag
        else:
            fresh["access"] = "FICTION-RENEWED-ACCESS-" + tag
            fresh["refresh"] = "FICTION-RENEWED-REFRESH-" + tag
            fresh["expires"] = FAR_FUTURE_MS
        _log("exit", provider=provider, account=account)
        return fresh


def factory(root, *, project):
    return Refresher()

"""Test-only seams for tests/test_oa_*.py, loaded into the `multiagents run` subprocess.

Active only when OA_FAKE_DIR is set (the test puts this directory on PYTHONPATH):
  * time.sleep of 10 s or more is virtual: it advances a counter in
    $OA_FAKE_DIR/vtime and returns at once; steps in sleep_script.json fire
    when the counter reaches their `after` (rewriting a profile's usage cache);
    past 400 virtual hours or 80 long sleeps it raises KeyboardInterrupt;
  * DNS resolution and a network connect (not AF_UNIX) fail and are logged
    to net-attempts, even on machines without working DNS;
  * OA_CRASH_CFG: starting the fake `claude` for a launch under that
    CLAUDE_CONFIG_DIR kills this process before the child exists.
"""
import json
import os
import socket
import subprocess
import sys
import time

_dir = os.environ.get("OA_FAKE_DIR")
if _dir:
    _real_sleep = time.sleep
    _state = {"v": 0.0, "n": 0, "done": set()}

    def usage_payload(used, reset_in):
        import datetime
        reset = (datetime.datetime.now(datetime.timezone.utc)
                 + datetime.timedelta(seconds=reset_in)).isoformat()
        return {"cachedUsageUtilization": {
            "fetchedAtMs": time.time() * 1000,
            "utilization": {"limits": [{"kind": "session", "percent": used,
                                        "resets_at": reset}]}}}

    def _apply(step):
        for item in step.get("profiles", []):
            path = os.path.join(item["state"])
            try:
                data = json.load(open(path))
            except (OSError, ValueError):
                data = {}
            data.update(usage_payload(item["used"], item["reset_in"]))
            json.dump(data, open(path, "w"))

    def _sleep(seconds):
        running = any(a in ("run", "resume") for a in sys.argv[1:])
        if not running or seconds < 10:
            return _real_sleep(seconds)
        _state["v"] += seconds
        _state["n"] += 1
        open(os.path.join(_dir, "vtime"), "w").write(str(_state["v"]))
        with open(os.path.join(_dir, "sleeps.log"), "a") as log:
            log.write(f"{seconds} {_state['v']}\n")
        try:
            steps = json.load(open(os.path.join(_dir, "sleep_script.json")))
        except (OSError, ValueError):
            steps = []
        for i, step in enumerate(steps):
            if i not in _state["done"] and _state["v"] >= step["after"]:
                _state["done"].add(i)
                _apply(step)
        stop_at = os.environ.get("OA_INTERRUPT_AT_SLEEP")
        if stop_at and _state["n"] >= int(stop_at):
            raise KeyboardInterrupt        # the user's Ctrl-C during the wait
        if _state["v"] > 400 * 3600 or _state["n"] > 80:
            raise KeyboardInterrupt

    time.sleep = _sleep

    _connect = socket.socket.connect

    def _no_net(self, address):
        if self.family != getattr(socket, "AF_UNIX", -1):
            open(os.path.join(_dir, "net-attempts"), "a").write(repr(address) + "\n")
            raise OSError("network disabled by the test")
        return _connect(self, address)

    socket.socket.connect = _no_net

    def _no_dns(host, port, *args, **kwargs):
        with open(os.path.join(_dir, "net-attempts"), "a") as log:
            log.write(repr((host, port)) + "\n")
        raise socket.gaierror("network disabled by the test")

    socket.getaddrinfo = _no_dns

    crash = os.environ.get("OA_CRASH_CFG")
    if crash:
        _init = subprocess.Popen.__init__

        def _popen(self, args, *a, **k):
            env = k.get("env") or {}
            argv = args if isinstance(args, (list, tuple)) else [args]
            if (os.path.basename(str(argv[0])) == "claude" and "--model" in argv
                    and env.get("CLAUDE_CONFIG_DIR") == crash):
                os._exit(137)
            _init(self, args, *a, **k)

        subprocess.Popen.__init__ = _popen

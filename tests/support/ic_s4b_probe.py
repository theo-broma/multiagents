"""The K4B.2 test readiness probe: loaded host-side through MULTIAGENTS_TEST_GATEWAY_PROBE.

Fake Docker starts no HTTP listener, so the real authenticated check cannot
succeed under it; the inspect-based identity and attachment checks still run.
"""


def factory():
    def probe(*args, **kwargs):
        return True
    return probe

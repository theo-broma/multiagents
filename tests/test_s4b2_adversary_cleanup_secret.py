"""Adversary, slice 4B.2: attempt cleanup retains the raw auth bearer on host disk.

HOLE (severity MEDIUM, credential retention / incomplete cleanup):
When an isolated attempt using `gateway_auth: anthropic` finishes and its resources
are cleaned up, the host retains the raw plaintext auth bearer token on disk indefinitely.

In `src/multiagents/executor/isolated.py`:
`_before_remove` (line 220) and `_resolve_failure` (line 277) invoke:
    await self.gateway_registry().cleanup(self.network_attempt)
`gateway_registry(service="egress")` defaults to `"egress"`. It never invokes
`self.gateway_registry("auth").cleanup(self.network_attempt)`.

Because the auth registry's `cleanup()` is never called:
1. The plaintext auth bearer file at `paths.scheduler / "gateways" / "auth" / "secrets" / attempt_id`
   is never unlinked.
2. The attempt record in `gateways/auth/authority.json` is never updated with `cleanup: True`.

Furthermore, in `src/multiagents/scheduler/gateways.py:603`:
    network = await self.network_metadata(record["network_id"])
    if isinstance(network, Absent):
        return None
    ...
    (self.root / "secrets" / attempt_id).unlink(missing_ok=True)
even if a caller invokes `auth.cleanup()` after `egress.cleanup()`, because `egress.cleanup()`
already removed the Docker network, `auth.cleanup()` finds the network `Absent` and returns
early at line 603, skipping the secret file unlinking at line 628.

IC-C29: "Once termination is confirmed, disconnect the attempt container and the gateways
from its network, then remove that network even if the exited container, output and
repository remain retained... Keep no raw credential once it is no longer needed for
recovery; preserve only the redaction knowledge required for retained output."
K4B2.1: "ending the attempt revokes both; cleanup checks the ownership of both gateways'
memberships."
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_s4b2 as ab  # noqa: E402


@pytest.fixture
def asd(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch)
    yield s
    s.close()


def test_s4b2_adversary_attempt_cleanup_unlinks_raw_auth_bearer_secret(asd):
    """MEDIUM: raw auth bearer secret file is retained on host disk after attempt cleanup."""
    asd.start()
    node = asd.launch()
    container = asd.running(node)
    run = asd.runs(node)[-1]
    attempt_id = run["attempt_id"]

    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(asd.root)

    egress_secret = paths.scheduler / "gateways" / "secrets" / attempt_id
    auth_secret = paths.scheduler / "gateways" / "auth" / "secrets" / attempt_id

    assert egress_secret.exists(), "egress password file was not created"
    assert auth_secret.exists(), "auth bearer file was not created"

    # Finish the attempt cleanly
    asd.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(asd, node)

    # After cleanup, the egress secret is deleted
    assert not egress_secret.exists(), "egress secret was not deleted after cleanup"

    # But the raw auth bearer token is stranded on disk because auth cleanup is never invoked
    assert not auth_secret.exists(), (
        f"raw auth bearer secret file {auth_secret} persists on disk after attempt cleanup: "
        f"isolated.py cleans up only the default (egress) gateway registry"
    )

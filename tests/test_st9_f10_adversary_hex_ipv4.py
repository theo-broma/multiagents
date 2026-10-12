"""ST9-R1/F10: noncanonical IPv4 must never become an IP suffix rule."""

import socket

import pytest

from multiagents.executor.docker import DockerExecutor
from st_docker import admits


@pytest.mark.parametrize("entry, destination", [
    ("0x1.1", "10.0.0x1.1"),
    ("0X1.1", "192.168.0x1.1"),
    ("0x7f.1", "10.0x7f.1"),
    ("０ｘ１．１", "172.16.0x1.1"),
])
def test_st9_r1_f10_hex_ipv4_fragment_cannot_admit_another_address(
        tmp_path, capsys, entry, destination):
    # inet_aton parses locally; this does not resolve DNS or contact anything.
    # These are different IPv4 addresses, not invented DNS hostnames.
    normalized = entry.encode("idna").decode("ascii").lower()
    assert socket.inet_aton(normalized) != socket.inet_aton(destination)
    ex = DockerExecutor({"egress_allowlist": [entry, "example.com"]})
    directory = ex.write_proxy_config(tmp_path / "proxy")
    applied = (directory / "filter").read_text()
    assert not admits(applied, destination), (entry, destination, applied)
    assert not admits(applied, normalized), applied
    assert admits(applied, "api.example.com"), applied
    assert "refused allowlist entry" in capsys.readouterr().err

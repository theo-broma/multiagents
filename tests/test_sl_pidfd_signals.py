"""SL-R7/R9: signal captured identities without a pid-reuse race."""
from __future__ import annotations

import errno
import signal
from unittest.mock import Mock, call

import pytest

from multiagents.scheduler import owner


@pytest.fixture
def syscalls(monkeypatch):
    calls = Mock()
    calls.open.return_value = 23
    calls.identity.return_value = "100"
    monkeypatch.setattr(owner.os, "pidfd_open", calls.open, raising=False)
    monkeypatch.setattr(owner.signal, "pidfd_send_signal", calls.send, raising=False)
    monkeypatch.setattr(owner.os, "close", calls.close)
    monkeypatch.setattr(owner.os, "kill", calls.kill)
    monkeypatch.setattr(owner, "_identity", calls.identity)
    return calls


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_sl_r7_r9_identity_is_checked_with_pidfd_open(syscalls, sig):
    owner._signal({521: "100"}, sig)
    assert syscalls.mock_calls == [call.open(521), call.identity(521),
                                   call.send(23, sig), call.close(23)]


@pytest.mark.parametrize("identity", ["", "101"])
def test_sl_r7_r9_gone_or_recycled_pid_is_not_signalled(syscalls, identity):
    syscalls.identity.return_value = identity
    owner._signal({521: "100"}, signal.SIGTERM)
    syscalls.send.assert_not_called()
    syscalls.kill.assert_not_called()
    syscalls.close.assert_called_once_with(23)


@pytest.mark.parametrize("stage", ["open", "send"])
@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_sl_r7_r9_gone_or_inaccessible_target_is_skipped(syscalls, stage, error):
    getattr(syscalls, stage).side_effect = error()
    owner._signal({521: "100"}, signal.SIGKILL)
    syscalls.kill.assert_not_called()
    if stage == "open":
        syscalls.identity.assert_not_called()
        syscalls.close.assert_not_called()
    else:
        syscalls.close.assert_called_once_with(23)


def test_sl_r7_r9_exit_after_identity_check_never_signals_replacement(syscalls):
    def exited(fd, sig):
        # The original exits and its numeric pid is reused after validation.
        syscalls.identity.return_value = "101"
        raise ProcessLookupError(errno.ESRCH, "original exited")

    syscalls.send.side_effect = exited
    owner._signal({521: "100"}, signal.SIGTERM)
    syscalls.send.assert_called_once_with(23, signal.SIGTERM)
    syscalls.kill.assert_not_called()
    syscalls.close.assert_called_once_with(23)


@pytest.mark.parametrize("stage", ["open", "send"])
@pytest.mark.parametrize("missing", [False, True])
def test_sl_r7_r9_unavailable_pidfd_falls_back(syscalls, monkeypatch, stage, missing):
    if missing:
        module, name = ((owner.os, "pidfd_open") if stage == "open"
                        else (owner.signal, "pidfd_send_signal"))
        monkeypatch.delattr(module, name)
    else:
        getattr(syscalls, stage).side_effect = OSError(errno.ENOSYS, "unavailable")
    owner._signal({521: "100"}, signal.SIGTERM)
    syscalls.kill.assert_called_once_with(521, signal.SIGTERM)
    assert syscalls.mock_calls[-2:] == [call.identity(521), call.kill(521, signal.SIGTERM)]
    if stage == "send":
        syscalls.close.assert_called_once_with(23)
    else:
        syscalls.close.assert_not_called()


def test_sl_r7_r9_fallback_revalidates_identity(syscalls):
    syscalls.send.side_effect = OSError(errno.ENOSYS, "unavailable")
    syscalls.identity.side_effect = ["100", "101"]
    owner._signal({521: "100"}, signal.SIGTERM)
    syscalls.kill.assert_not_called()
    syscalls.close.assert_called_once_with(23)


@pytest.mark.parametrize("stage", ["open", "send"])
def test_sl_r7_r9_other_pidfd_errors_do_not_allow_bare_pid_signals(syscalls, stage):
    getattr(syscalls, stage).side_effect = OSError(errno.EMFILE, "too many open files")
    with pytest.raises(OSError) as raised:
        owner._signal({521: "100"}, signal.SIGTERM)
    assert raised.value.errno == errno.EMFILE
    syscalls.kill.assert_not_called()
    if stage == "send":
        syscalls.close.assert_called_once_with(23)
    else:
        syscalls.close.assert_not_called()

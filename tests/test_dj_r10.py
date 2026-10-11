"""DJ-R10: only the live script's dry run belongs in automated tests."""
import os
import subprocess
from test_dj_support import ROOT, CLI_BOUND


def test_dj_r10_live_check_dry_run_never_invokes_claude(tmp_path):
    script = ROOT / 'scripts/dj-live-check.sh'
    assert script.is_file(), 'DJ live evidence script is missing'
    fakebin = tmp_path / 'bin'; fakebin.mkdir()
    marker = tmp_path / 'claude-called'
    fake = fakebin / 'claude'
    fake.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
    fake.chmod(0o755)
    result = subprocess.run(['sh',str(script),'--dry-run'],
        env=dict(os.environ,PATH=str(fakebin)+os.pathsep+os.environ.get('PATH','')),
        cwd=tmp_path,capture_output=True,text=True,timeout=CLI_BOUND)
    assert not marker.exists(), 'dry run invoked claude'
    assert result.returncode == 0, result.stderr

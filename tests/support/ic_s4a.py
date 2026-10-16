"""Slice 4A black-box probes, delivered through the provider's normal CLI."""
import json
import os
import socket
import stat
import uuid
from pathlib import Path
import pytest
import ic_probe as probe
import ic_s3 as s3
import ic_docker as dk

WAIT = 4
LOOP_WAIT = 8  # several launches and loop transitions, still bounded
DOCKER = pytest.param('docker', marks=pytest.mark.skipif(not dk.OPT_IN, reason='SG_TEST_DOCKER=1 required'))

# These branches extend the existing probe protocol, without changing its users.
EXTRA = r'''
if probe == "scoped_rpc":
    path = os.environ.get("MULTIAGENTS_RPC_SOCKET")
    replies = []
    revision = None
    for req in ins.get("requests", []):
        if req.get("args", {}).get("plan_revision") == "CURRENT" and revision is None:
            c = socket.socket(socket.AF_UNIX); c.settimeout(2); c.connect(path)
            c.sendall(json.dumps({"op":"list_nodes", "args":{}, "request_id":"revision"}).encode()+b"\n")
            raw = b""
            while b"\n" not in raw: raw += c.recv(65536)
            revision = json.loads(raw.split(b"\n")[0])["result"]["plan_revision"]; c.close()
        if req.get("args", {}).get("plan_revision") == "CURRENT": req["args"]["plan_revision"] = revision
        try:
            c = socket.socket(socket.AF_UNIX); c.settimeout(2); c.connect(path)
            c.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(65536)
                if not chunk: break
                buf += chunk
            replies.append(json.loads(buf.split(b"\n")[0])); c.close()
        except Exception as e: replies.append({"transport_error": str(e)})
    directory = os.path.dirname(path) if path else ""
    say(dict(common(), endpoint=path, replies=replies,
             entries=os.listdir(directory) if directory else [],
             hidden={p:os.path.exists(p) for p in ins.get("hidden_paths",[])},
             writable=attempt(lambda: open(os.path.join(directory, "forged"), "w").write("x")) if ins.get("check_readonly") else None,
             rpc_env={k:v for k,v in os.environ.items() if "RPC" in k or "TOKEN" in k}))
    if ins.get("hold"):
        import signal
        signal.pause()
elif probe == "session":
    import uuid
    args = sys.argv[1:]
    resumed = args[args.index("-s")+1] if "-s" in args else None
    # SES was chosen before the first provider event.
    store = os.path.join(HOME, "sessions")
    payload = os.path.join(store, "payload.json")
    try: prior = json.load(open(payload))
    except Exception: prior = None
    if resumed and ins.get("corrupt") and prior is None:
        print("corrupt session payload", file=sys.stderr); sys.exit(1)
    if resumed and ins.get("confirm", True):
        if ins.get("wrong_confirmation"):
            recorded = SES; SES = "ses_wrong"
            emit("session_resumed", {"id":"resume", "type":"session-resumed"}); SES = recorded
        else:
            emit("session_resumed", {"id":"resume", "type":"session-resumed"})
    if resumed and ins.get("reject"):
        print("session rejected", file=sys.stderr); sys.exit(1)
    os.makedirs(store, exist_ok=True)
    json.dump({"session":SES, "nonce":ins.get("nonce", "payload")}, open(payload,"w"))
    if ins.get("forbidden"):
        open(os.path.join(store,"credential.secret"),"w").write("DO-NOT-PROJECT")
    if ins.get("corrupt"):
        open(payload,"w").write("{corrupt")
    if ins.get("missing"):
        os.remove(payload); os.rmdir(store)
    say(dict(common(), session=SES, resumed=resumed, prior=prior,
             files=sorted(os.listdir(HOME)), session_files=sorted(os.listdir(store)) if os.path.isdir(store) else []))
    if ins.get("hold") or (resumed and ins.get("resume_hold")):
        import signal
        signal.pause()
elif probe == "plant":
'''

class Provider(probe.ProbeProvider):
    def __init__(self, tmp, name, *, mode='none', rpc=False):
        super().__init__(tmp, name)
        self.entry['needs'] = {'credentials':False, 'network':False, 'rpc':rpc}
        self.entry['session'] = {'mode':mode, 'resumed_event':'session_resumed', 'resume_deadline_seconds':0.25}
        if mode != 'none': self.entry['session']['path'] = 'sessions'
        script = Path(self.entry['bin'])
        source = probe.PROBE_CLI.replace('if probe == "plant":\n', EXTRA, 1)
        source = source.replace('SES = "ses_ic_%d" % os.getpid()',
            'SES = (sys.argv[sys.argv.index("-s")+1] if "-s" in sys.argv else "ses_ic_%d" % os.getpid())')
        script.write_text(source)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

class World(s3.S3Sched):
    def configure(self, mode='none', rpc=False):
        provider = Provider(self.tmp, 'acme', mode=mode, rpc=rpc)
        self.p.fakes['acme'] = provider
        self.p.providers['acme'] = provider.entry
        self.fake = provider
        self.p.agents['worker'].update(can_spawn=True, max_children=8)
        self.p.write()
        self._reset_server()

    def launch(self, kind, **fields):
        return self.create(task=probe.instruction(kind, **fields))['id']

    def reports(self, node):
        rows = []
        for run in self.runs(node):
            out, _ = self.host_output(run['run_id'])
            for line in out.decode(errors='replace').splitlines():
                try: text = json.loads(line).get('part', {}).get('text', '')
                except ValueError: continue
                if text.startswith('IC-REPORT '): rows.append(json.loads(text[10:]))
        return rows

    def report(self, node, n=1):
        return self.wait(lambda: len(self.reports(node)) >= n and self.reports(node)[n-1], WAIT, 'probe report')

    def reopen(self, node, **fields):
        reply = self.rpc('relaunch_node', {'id':node, 'revision':self.get(node)['revision'], **fields})
        assert reply.get('ok') is True, reply
        return reply


def request(op='list_nodes', args=None, **extra):
    return dict(op=op, args=args or {}, request_id=uuid.uuid4().hex, **extra)


def resume(s, node):
    runs = s.runs(node)
    assert runs, s.get(node)
    assert 'session_resume' in runs[-1], 'IC-C23: run must record session_resume'
    return runs[-1]['session_resume']


def make_world(request, tmp_path, monkeypatch):
    owner = None
    if request.param == 'docker':
        dk.require_docker(); owner = dk.test_owner(request)
    s = World(tmp_path, monkeypatch, backend=request.param, owner=owner)
    try:
        yield s
    finally:
        s.close()
        if owner: dk.remove_session_resources(owner)


def connect(path):
    c = socket.socket(socket.AF_UNIX); c.settimeout(1); c.connect(path); return c


def exchange(c, req):
    c.sendall(json.dumps(req).encode()+b'\n')
    data = b''
    while b'\n' not in data:
        part = c.recv(65536)
        if not part: raise ConnectionError('closed')
        data += part
    return json.loads(data.split(b'\n')[0])

# The loop fixture keeps its normal public verdict protocol; only provider
# behavior is extended. It runs on the reference backend, which permits host
# fixture controls; containment is tested separately on Docker.
def loop_world(tmp_path, monkeypatch, mode='export', kind='isolated', payload=True, recorded=True, plant_forbidden=False):
    import ic_prov as ip
    w = ip.make_world(tmp_path, monkeypatch, kind=kind)
    w.pair()
    for provider in (w.fxw, w.fxr):
        s3.declare(provider.entry, rpc=True)
    p = w.fxr
    p.entry['session'] = {'mode':mode, 'path':'sessions', 'resumed_event':'session_resumed', 'resume_deadline_seconds':0.25}
    p.entry['session_store'] = {'dir':'sessions', 'glob':'*.json', 'forbidden_profile':'sessions/*.secret'}
    script = p.dir / 'agent.py'
    source = script.read_text()
    extra = r'''
store = os.path.join(os.environ['HOME'], 'sessions')
prior = None
try: prior = json.load(open(os.path.join(store,'payload.json')))
except Exception: pass
if resume:
    emit('session_resumed', {'id':'resume', 'type':'session-resumed'})
seen_files = sorted(os.listdir(os.environ['HOME']))
seen_session_files = sorted(os.listdir(store)) if os.path.isdir(store) else []
if PAYLOAD:
    os.makedirs(store, exist_ok=True)
    json.dump({'session':session,'nonce':'alias-payload'}, open(os.path.join(store,'payload.json'),'w'))
    if PLANT_FORBIDDEN: open(os.path.join(store,'credential.secret'),'w').write('forbidden')
open(os.path.join(os.environ['HOME'],'unrelated-profile'),'w').write('legacy-profile')
with open(base + '/sessions.jsonl','a') as report:
    report.write(json.dumps({'tag':fx.get('tag'), 'home':os.environ['HOME'], 'prior':prior,
        'files':seen_files, 'session_files':seen_session_files})+'\n')
'''.replace('PAYLOAD', repr(payload)).replace('PLANT_FORBIDDEN', repr(plant_forbidden))
    marker = 'emit("step_start",'
    source = source.replace(marker, extra+'\n'+marker, 1)
    if not recorded:
        source = source.replace('dict(part, sessionID=session)', 'dict(part)').replace('"sessionID": session, ', '')
    script.write_text(source)
    w.write_config()
    return w


def loop_reports(w):
    path = w.fxr.dir / 'sessions.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def payload_files(root, session):
    """Find a probe's exact payload without assuming a storage layout."""
    found = []
    for path in Path(root).rglob('payload.json'):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get('session') == session: found.append(path)
    return found


def host_endpoint(s, guest_path):
    if s.backend_name == 'reference': return guest_path
    import subprocess
    import ic_support as ic
    owned = ic.run(s.be().list_owned(s.slug))
    assert len(owned) == 1, owned
    identity = ic.identity_of(owned[0])
    inspected = subprocess.run(['docker','inspect', identity.id], capture_output=True, text=True, timeout=WAIT)
    assert inspected.returncode == 0, inspected.stderr
    mounts = json.loads(inspected.stdout)[0]['Mounts']
    mount = next(m for m in mounts if m['Destination'] == '/run/multiagents/rpc')
    return str(Path(mount['Source']) / 'rpc.sock')

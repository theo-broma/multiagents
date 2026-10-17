"""Trusted, self-contained Docker log producer (D-S4B.7 and K4B.7)."""

# This code is supplied by the host as Python's -c argument. Its only changing
# inputs, including redaction literals, live on the private read-only mount.
ENTRYPOINT = r'''
import base64, ctypes, errno, json, os, selectors, signal, subprocess, time

os.umask(0o077)
TEST_MODE = os.environ.get('MULTIAGENTS_TEST_REDACTOR_UNPRIVILEGED') == '1'
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
DRAIN = 1.5
MARKER = b'[REDACTED]'

def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]

class Redactor:
    def __init__(self, secrets):
        self.root = {}
        self.pending = b''
        for secret in secrets:
            if not isinstance(secret, str) or not secret:
                raise ValueError('invalid redaction literal')
            node = self.root
            for byte in secret.encode():
                node = node.setdefault(byte, {})
            node[None] = True

    def feed(self, data, final=False):
        data = self.pending + data
        self.pending = b''
        result = bytearray()
        start = 0
        while start < len(data):
            node, end, match = self.root, start, None
            while end < len(data) and data[end] in node:
                node = node[data[end]]
                end += 1
                if None in node:
                    match = end
            if end == len(data) and any(key is not None for key in node) and not final:
                self.pending = data[start:]
                break
            if match is not None:
                result.extend(MARKER)
                start = match
            else:
                result.append(data[start])
                start += 1
        return bytes(result)

def emit(prefix, fd, data):
    if data:
        write_all(fd, prefix + base64.b64encode(data) + b'\n')

def close_except(keep):
    # /proc/self/fd includes the directory descriptor, which is already closed
    # when listdir returns. Never depend on a guessed descriptor upper bound.
    for name in os.listdir('/proc/self/fd'):
        fd = int(name)
        if fd not in keep:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

def drop(uid, gid, relay=False):
    libc = ctypes.CDLL(None, use_errno=True)
    def prctl(option, *args):
        if libc.prctl(option, *(args + (0,) * (4 - len(args)))) == -1:
            raise OSError(ctypes.get_errno(), 'privilege separation unavailable')
    prctl(38, 1)                         # PR_SET_NO_NEW_PRIVS
    prctl(47, 4)                         # PR_CAP_AMBIENT_CLEAR_ALL
    for cap in range(64):
        present = libc.prctl(23, cap, 0, 0, 0)  # PR_CAPBSET_READ
        if present == -1:
            if ctypes.get_errno() == errno.EINVAL:
                break
            raise OSError('cannot read capability bounding set')
        if present:
            prctl(24, cap)               # PR_CAPBSET_DROP, needs SETPCAP
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    if relay:
        # kill(2) compares the sender's effective UID to the target's real or
        # saved UID. This relay can signal the host-UID child, while that child
        # cannot signal the relay (whose real and saved UIDs remain root).
        os.setresuid(0, uid, 0)
    else:
        os.setresuid(uid, uid, uid)
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Caps(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32),
                    ('inheritable', ctypes.c_uint32)]
    header, caps = Header(0x20080522, 0), (Caps * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(caps)) != 0:
        raise OSError('cannot clear capabilities')
    prctl(4, 0)                          # PR_SET_DUMPABLE
    expected = (0, uid, 0) if relay else (uid, uid, uid)
    if os.getresuid() != expected or os.getresgid() != (gid, gid, gid) or os.getgroups():
        raise OSError('identity drop failed')
    with open('/proc/self/status') as stream:
        status = dict(line.rstrip().split(':', 1) for line in stream if ':' in line)
    for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
        if int(status[name], 16):
            raise OSError('capability drop failed')
    if status['NoNewPrivs'].strip() != '1':
        raise OSError('no-new-privileges unavailable')

def capture(invocation, pipes, status_fd, secrets):
    prefix = invocation['output_prefix'].encode('ascii')
    selector = selectors.DefaultSelector()
    redactors = {}
    for pipe, fd in pipes:
        redactors[fd] = Redactor(secrets)
        selector.register(pipe, selectors.EVENT_READ, fd)
    selector.register(status_fd, selectors.EVENT_READ, None)
    status, code = b'', None
    while selector.get_map():
        ready = selector.select(0.05)
        if not ready and code is not None:
            # Descendants may retain pipe descriptors after the command exits.
            break
        for key, events in ready:
            data = os.read(key.fd, 2048)
            if key.data is None:
                status += data
                if not data or b'\n' in status:
                    try:
                        code = int(status)
                    except ValueError:
                        code = 125
                    selector.unregister(key.fd)
                    os.close(key.fd)
            else:
                emit(prefix, key.data, redactors[key.data].feed(data, final=not data))
                if not data:
                    selector.unregister(key.fd)
                    os.close(key.fd)
    for fd, redactor in redactors.items():
        emit(prefix, fd, redactor.feed(b'', final=True))
    selector.close()
    return code if code is not None else 125

def separated(invocation, stdin_fd, secrets):
    uid, gid = invocation['uid'], invocation['gid']
    if type(uid) is not int or type(gid) is not int or not 0 < uid < 2**32 - 1 or not 0 <= gid < 2**32 - 1:
        raise ValueError('invalid host identity')
    if os.geteuid() != 0:
        raise OSError('privilege separation requires root setup')
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    signal_r, signal_w = os.pipe()
    status_r, status_w = os.pipe()
    ready_r, ready_w = os.pipe()
    os.set_blocking(signal_w, False)
    def forward(signum, frame):
        try:
            os.write(signal_w, bytes([signum]))
        except OSError:
            pass
    for signum in SIGNALS:
        signal.signal(signum, forward)
    relay = os.fork()
    if relay == 0:
        try:
            # Neither the relay nor the workload may inherit Docker's raw log
            # descriptors, even temporarily. Only PID 1 retains those handles.
            null = os.open('/dev/null', os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            close_except({0, 1, 2, stdin_fd, out_w, err_w, signal_r, status_w, ready_r})
            for signum in SIGNALS:
                signal.signal(signum, signal.SIG_IGN)
            # Orphaned guest descendants reparent here, not to PID 1, so the
            # relay sees when the whole workload has drained.
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == -1:  # PR_SET_CHILD_SUBREAPER
                raise OSError(ctypes.get_errno(), 'child subreaper unavailable')
            child = os.fork()
            if child == 0:
                try:
                    for signum in SIGNALS:
                        signal.signal(signum, signal.SIG_DFL)
                    # kill(2) permits SIGCONT within one session whatever the
                    # UIDs. A session of its own keeps the guest from reaching
                    # PID 1 or the relay, and gives the relay one group to stop.
                    os.setsid()
                    os.dup2(stdin_fd, 0)
                    os.dup2(out_w, 1)
                    os.dup2(err_w, 2)
                    close_except({0, 1, 2, ready_r})
                    drop(uid, gid)
                    os.chdir(invocation['cwd'])
                    if os.read(ready_r, 1) != b'1':
                        os._exit(125)
                    os.close(ready_r)
                    os.execvpe(invocation['argv'][0], invocation['argv'], invocation['env'])
                except BaseException:
                    os._exit(125)
            close_except({0, 1, 2, signal_r, status_w})
            drop(uid, gid, relay=True)
            write_all(status_w, b'1')
            def relay_signal(signum):
                # The guest's own processes share the child's group. Before the
                # child's setsid that group does not exist yet; once the child
                # is reaped its PID may be reused, so never signal it alone.
                try:
                    os.killpg(child, signum)
                except ProcessLookupError:
                    if status is None:
                        try:
                            os.kill(child, signum)
                        except ProcessLookupError:
                            pass
            def reap():
                # Returns False once no descendant is left at all.
                nonlocal status
                while True:
                    try:
                        done, value = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        return False
                    if not done:
                        return True
                    if done == child:
                        status = value
            selector = selectors.DefaultSelector()
            selector.register(signal_r, selectors.EVENT_READ)
            status, deadline, terminated = None, None, False
            while True:
                remaining = reap()
                if status is not None and deadline is None:
                    # Give the rest of the guest a bounded grace, well inside
                    # Docker's stop timeout, before reporting the exit.
                    deadline = time.monotonic() + DRAIN
                    if remaining and not terminated:
                        relay_signal(signal.SIGTERM)
                        terminated = True
                if deadline is not None and (not remaining or time.monotonic() >= deadline):
                    if remaining:
                        relay_signal(signal.SIGKILL)
                    write_all(status_w, str(os.waitstatus_to_exitcode(status)).encode() + b'\n')
                    os._exit(0)
                for key, events in selector.select(0.05):
                    data = os.read(signal_r, 1024)
                    if not data:
                        relay_signal(signal.SIGKILL)
                        os._exit(125)
                    for signum in data:
                        if signum in SIGNALS:
                            relay_signal(signum)
                            terminated = terminated or signum == signal.SIGTERM
        except BaseException:
            os._exit(125)
    close_except({0, 1, 2, out_r, err_r, signal_w, status_r, ready_w})
    redactor_uid = 65534 if uid != 65534 else 65533
    redactor_gid = 65534 if gid != 65534 else 65533
    drop(redactor_uid, redactor_gid)
    if os.read(status_r, 1) != b'1':
        raise OSError('signal relay setup failed')
    write_all(ready_w, b'1')
    os.close(ready_w)
    code = capture(invocation, ((out_r, 1), (err_r, 2)), status_r, secrets)
    os.close(signal_w)
    os.waitpid(relay, 0)
    return code

def ordinary(invocation, stdin_fd, secrets):
    # The explicit test seam also exercises the real streaming redactor. The
    # ordinary no-credential path retains slice 3's unprivileged entrypoint.
    os.chdir(invocation['cwd'])
    child, queued = None, []
    def forward(signum, frame):
        if child is None:
            queued.append(signum)
        else:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
    for signum in SIGNALS:
        signal.signal(signum, forward)
    child = subprocess.Popen(invocation['argv'], env=invocation['env'], stdin=stdin_fd,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(stdin_fd)
    for signum in queued:
        forward(signum, None)
    prefix = invocation['output_prefix'].encode('ascii')
    selector = selectors.DefaultSelector()
    redactors = {1: Redactor(secrets), 2: Redactor(secrets)}
    for pipe, fd in ((child.stdout, 1), (child.stderr, 2)):
        selector.register(pipe, selectors.EVENT_READ, fd)
    while selector.get_map():
        ready = selector.select(0.05)
        if not ready and child.poll() is not None:
            break
        for key, events in ready:
            data = os.read(key.fd, 2048)
            emit(prefix, key.data, redactors[key.data].feed(data, final=not data))
            if not data:
                selector.unregister(key.fileobj)
                key.fileobj.close()
    for fd, redactor in redactors.items():
        emit(prefix, fd, redactor.feed(b'', final=True))
    selector.close()
    return child.wait()

def main():
    # The 0700 input belongs to the host UID. Root setup deliberately has no
    # DAC override capability, so read/open it using that owner's effective UID
    # before returning to root for the trusted forks.
    root = os.geteuid() == 0 or os.getuid() == 0
    if TEST_MODE and root:
        # A root start is the production start; the variable can come from
        # the image's ENV, so never run the child unseparated (K4B.7).
        os._exit(125)
    switch = False
    if os.geteuid() == 0:
        owner = os.stat('/multiagents-input').st_uid
        if owner != 0:
            os.seteuid(owner)
            switch = True
    with open('/multiagents-input/invocation.json') as stream:
        invocation = json.load(stream)
    source = invocation.get('stdin')
    stdin_fd = os.open(source if source else '/dev/null', os.O_RDONLY)
    if switch:
        os.seteuid(0)
    secrets = invocation.get('redaction_secrets', [])
    # Validate before running any guest code, including the unprivileged seam.
    Redactor(secrets)
    if root and not secrets:
        # Every root start separates or fails, before any child exists (K4B.9).
        raise OSError('privilege separation requires redaction secrets')
    if root or (secrets and not TEST_MODE):
        return separated(invocation, stdin_fd, secrets)
    return ordinary(invocation, stdin_fd, secrets)

try:
    code = main()
    raise SystemExit(code if code >= 0 else 128 - code)
except Exception:
    # Exceptions can embed argv or input values. Never let a traceback bypass
    # the redactor, including failures partway through the trusted setup.
    os._exit(125)
'''

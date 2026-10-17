"""Host-only detached commands and at-least-once root notifications (DJ)."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from . import decisions
from .paths import ProjectPaths, state_root
from .procs import start_time
from .tree import Tree

ROLES = ('orchestrator', 'initializer')
ENDED = {'finished', 'stopped', 'failed_to_start', 'lost'}
CONTROL_ENV = (
    'MULTIAGENTS_SAFEPOINT_KEY', 'MULTIAGENTS_SAFEPOINT_DIR',
    'MULTIAGENTS_SAFEPOINT_DRIVER', 'MULTIAGENTS_MCP_CONFIG',
    'MULTIAGENTS_MCP_COMMAND', 'MULTIAGENTS_MCP_ARGS',
    'MULTIAGENTS_SESSION_ID', 'MULTIAGENTS_ROLE', 'MULTIAGENTS_BG_SETTINGS',
)
FIELDS = {'id', 'key', 'name', 'owner', 'argv', 'cwd', 'origin_session',
          'pid', 'pid_start', 'started_at', 'state', 'exit_code', 'ended_at',
          'emitted_at', 'emitted_session', 'acked_at'}


def now():
    return float(os.environ.get('MULTIAGENTS_BG_NOW', time.time()))


def safe_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]+', value))


def living(pid, started):
    # Fail closed: an unverified pid never authorises a signal or a watcher.
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not started:
        return False
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
        return fields[0] not in ('Z', 'X') and fields[19] == started
    except (OSError, IndexError):
        return False


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f'Unsafe detached-job directory: {path}')
    path.chmod(0o700)


def read_text(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('not a regular file')
        return stream.read()


def read_json(path):
    try:
        return json.loads(read_text(path))
    except (OSError, ValueError):
        return None


def atomic(path, text):
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def write_json(path, value):
    atomic(path, json.dumps(value, sort_keys=True) + '\n')


class Registry:
    def __init__(self, paths):
        self.root = state_root().absolute() / 'bg' / paths.slug
        private_dir(self.root.parent)
        private_dir(self.root)

    def path(self, ident, suffix):
        if not safe_id(ident):
            raise ValueError('Invalid detached-job id')
        return self.root / f'{ident}.{suffix}'

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save(self, row):
        value = {k: v for k, v in row.items() if k != 'unacked'}
        write_json(self.path(row['id'], 'json'), value)

    def get(self, ident, persist=True):
        if not safe_id(ident):
            return None
        path = self.path(ident, 'json')
        if not path.exists() and not path.is_symlink():
            return None
        row = read_json(path)
        if not self.valid(row, ident):
            return {'id': ident, 'state': 'invalid', 'unacked': False}
        old = dict(row)
        status = None
        try:
            raw = read_text(self.path(ident, 'exit')).strip()
            if re.fullmatch(r'signal:[A-Z0-9]+', raw):
                status = raw
            else:
                status = int(raw)
        except (OSError, ValueError):
            pass
        if row['state'] == 'stopped':
            pass
        elif status is not None:
            row.update(state='finished', exit_code=status)
        elif row['state'] == 'failed_to_start':
            pass
        elif living(row['pid'], row['pid_start']):
            row['state'] = 'running'
        elif row['state'] == 'starting' and row['pid'] is None and now() - row['started_at'] <= 60:
            pass
        else:
            row['state'] = 'lost'
        if row['state'] in ENDED and row['ended_at'] is None:
            row['ended_at'] = now()
        if persist and row != old:
            self.save(row)
        row['unacked'] = row['state'] in ENDED and row['acked_at'] is None
        return row

    @staticmethod
    def valid(row, ident):
        if not isinstance(row, dict) or row.get('version') != 1 or not FIELDS <= row.keys():
            return False
        if row['id'] != ident or row['owner'] not in ROLES or not isinstance(row['argv'], list):
            return False
        if row['state'] not in ENDED | {'starting', 'running'}:
            return False
        if not all(isinstance(row[k], str) for k in ('name', 'cwd')):
            return False
        if not all(isinstance(v, str) for v in row['argv']):
            return False
        if not isinstance(row['started_at'], (int, float)):
            return False
        if any(row[k] is not None and not isinstance(row[k], (int, float))
               for k in ('ended_at', 'acked_at', 'emitted_at')):
            return False
        if row['pid'] is not None and (not isinstance(row['pid'], int) or row['pid'] <= 0):
            return False
        return all(row[k] is None or isinstance(row[k], str)
                   for k in ('pid_start', 'key', 'origin_session', 'emitted_session'))

    def rows(self, owner=None, persist=True):
        return [r for p in sorted(self.root.glob('*.json'))
                if safe_id(p.stem) and (r := self.get(p.stem, persist=persist))
                and (owner is None or r.get('owner') == owner)]

    def prune(self):
        removed = []
        for row in self.rows(persist=False):
            if row['state'] not in ENDED or row['acked_at'] is None or row['ended_at'] is None:
                continue
            if min(now() - row['acked_at'], now() - row['ended_at']) <= 7 * 86400:
                continue
            for suffix in ('json', 'exit', 'log'):
                with contextlib.suppress(FileNotFoundError):
                    self.path(row['id'], suffix).unlink()
            removed.append(row['id'])
        return removed

    def report(self, rows, heartbeat=False, cap=4000):
        chunks = ['Detached jobs heartbeat:' if heartbeat else 'Detached job report:']
        for row in rows:
            elapsed = max(0, (row.get('ended_at') or now()) - row['started_at'])
            chunks.append(f"{row['id']} {row['name']}: {row['state']}, exit {row['exit_code']}, duration {elapsed:.0f}s\nLog: {self.path(row['id'], 'log')}")
            if not heartbeat:
                try:
                    # Bound the read as well as the rendered tail.
                    fd = os.open(self.path(row['id'], 'log'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as stream:
                        if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 128 * 1024))
                            lines = stream.read().decode('utf-8', 'replace').split('\n')
                            if lines[-1] == '': lines.pop()
                            chunks.extend(''.join(c for c in line if c.isprintable())[:300]
                                          for line in lines[-20:])
                except OSError:
                    pass
        return ''.join(c for c in '\n'.join(chunks) if c.isprintable() or c == '\n')[:cap]


def module_command(module):
    # The driver may be running from a source checkout rather than an installed
    # package. Bootstrap that same absolute package root, independently of cwd.
    root = str(Path(__file__).resolve().parents[1])
    bootstrap = (f'import sys,runpy;sys.path.insert(0,{root!r});'
                 'module=sys.argv[2];sys.argv=sys.argv[2:];'
                 'runpy.run_module(module,run_name="__main__")')
    return [sys.executable, '-c', bootstrap, '-m', module]


def prepare_hook(paths, role, session_id, timeout):
    registry = Registry(paths)
    directory = registry.root / 'settings'
    private_dir(directory)
    if not safe_id(session_id):
        raise ValueError('Invalid root session id')
    command = module_command('multiagents.cli') + ['--path', str(paths.root), 'bg', 'watch',
        '--owner', role, '--timeout', str(timeout)]
    path = directory / f'{session_id}.json'
    write_json(path, {'hooks': {'Stop': [{'hooks': [{'type': 'command',
        'command': shlex.join(command), 'asyncRewake': True, 'timeout': timeout}]}]}})
    return path


def bind(paths, role, session_id, pid):
    registry = Registry(paths)
    if not safe_id(session_id):
        raise ValueError('Invalid root session id')
    with registry.lock():
        directory = registry.root / 'bindings'
        private_dir(directory)
        write_json(directory / f'{session_id}.json', dict(version=1, role=role,
            session_id=session_id, pid=pid, pid_start=start_time(pid)))


def run(registry, args):
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not argv:
        print('bg run requires a command', file=sys.stderr)
        return 1
    with registry.lock():
        registry.prune()
        if args.key:
            for row in registry.rows():
                if row.get('key') == args.key and row['state'] in ('starting', 'running'):
                    print(f"{row['id']} pid={row['pid']} log={registry.path(row['id'], 'log')}")
                    return 0
        session = os.environ.get('MULTIAGENTS_SESSION_ID')
        binding = read_json(registry.root / 'bindings' / f'{session}.json') if safe_id(session) else None
        if not (isinstance(binding, dict) and binding.get('version') == 1
                and binding.get('role') == args.owner and living(binding.get('pid'), binding.get('pid_start'))):
            session = None
        ident = uuid.uuid4().hex[:12]
        row = dict(version=1, id=ident, key=args.key, name=args.name or '', owner=args.owner,
                   argv=argv, cwd=str(Path.cwd()), origin_session=session, pid=None,
                   pid_start=None, started_at=now(), state='starting', exit_code=None,
                   ended_at=None, emitted_at=None, emitted_session=None, acked_at=None)
        registry.save(row)
        read_fd, write_fd = os.pipe()
        env = {k: v for k, v in os.environ.items() if k not in CONTROL_ENV}
        try:
            subprocess.Popen(module_command('multiagents.bg') +
                [str(registry.root), ident, str(write_fd), json.dumps(argv)],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(write_fd,))
            os.close(write_fd)
            write_fd = None
            with os.fdopen(read_fd) as stream:
                result = json.loads(stream.readline())
            if 'error' in result:
                raise OSError(result['error'])
            row.update(pid=result['pid'], pid_start=result['pid_start'], state='running')
            registry.save(row)
        except (OSError, ValueError) as exc:
            row.update(state='failed_to_start', ended_at=now())
            registry.save(row)
            print(f'Cannot execute {argv[0]}: {exc}', file=sys.stderr)
            return 1
        finally:
            if write_fd is not None: os.close(write_fd)
        print(f"{ident} pid={row['pid']} log={registry.path(ident, 'log')}")
    return 0


def supervise():
    root, ident, pipe, command = sys.argv[1:]
    # This process owns the exit status; the command owns a separate session
    # and process group so stopping it cannot kill the status writer.
    path = Path(root)
    with os.fdopen(int(pipe), 'w') as channel:
        try:
            fd = os.open(path / f'{ident}.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as log:
                child = subprocess.Popen(json.loads(command), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True)
        except OSError as exc:
            channel.write(json.dumps({'error': str(exc)}) + '\n')
            return
        channel.write(json.dumps({'pid': child.pid, 'pid_start': start_time(child.pid)}) + '\n')
    code = child.wait()
    result = str(code) if code >= 0 else 'signal:' + signal.Signals(-code).name.removeprefix('SIG')
    atomic(path / f'{ident}.exit', result + '\n')


def decision_report(records):
    """UD-C12: one line per answered decision due for delivery — its id, its
    title, the chosen option ids, then the answer's free text and its
    comment. Local to the session: nothing here reaches ntfy."""
    chunks = ['Answered decisions:']
    for rec in records:
        answer = rec.get('answer') or {}
        options = ', '.join(answer.get('options') or [])
        # both, when both were given: a comment alone told the asker the user
        # answered "(free text)" without ever saying what they answered
        text = ' — '.join(part for part in (
            ' '.join(str(answer.get('free_text') or '').split()),
            ' '.join(str(answer.get('comment') or '').split())) if part)
        if len(text) > 300:
            text = text[:299] + '…'
        line = (f"{rec['id']} {' '.join(str(rec.get('title') or '').split())}: "
                f"answered {options or '(free text)'}")
        if text:
            line += f" — {text}"
        chunks.append(''.join(c for c in line if c.isprintable()))
    return '\n'.join(chunks)


def watch(registry, args):
    started = now()
    try:
        session = json.load(sys.stdin).get('session_id')
    except (ValueError, AttributeError):
        return 0
    if not safe_id(session): return 0
    binding = read_json(registry.root / 'bindings' / f'{session}.json')
    if not (isinstance(binding, dict) and binding.get('version') == 1
            and binding.get('role') == args.owner and binding.get('session_id') == session
            and living(binding.get('pid'), binding.get('pid_start'))):
        return 0
    def cli_alive():
        return living(binding['pid'], binding['pid_start'])
    lock = registry.root / f'watch-{args.owner}.lock'
    holder = {'pid': os.getpid(), 'pid_start': start_time(os.getpid())}
    with registry.lock():
        previous = read_json(lock)
        if isinstance(previous, dict) and living(previous.get('pid'), previous.get('pid_start')):
            return 0
        write_json(lock, holder)
    timeout = args.timeout
    if timeout is None:
        from .config import load
        timeout = load(args.paths).project.get('bg', {}).get('watch_timeout', 3600)
    deadline = started + max(0, timeout - 60)
    tree = Tree(args.paths.tree_file, args.paths.events_file)
    try:
        while True:
            with registry.lock():
                if not cli_alive(): return 0
                rows = registry.rows(args.owner)
                due = [r for r in rows if r['unacked'] and (r['emitted_at'] is None
                    or r['emitted_session'] != session or now() - r['emitted_at'] > 900)]
                # UD-C11/UD-C12: answered decisions of this owner role are a
                # second notification source, reported in the same pass.
                # UD-C13: a decision record another writer damaged — or a
                # tree this pass cannot read at all — must never take the
                # job report down with it, so the pass cannot raise here.
                try:
                    wake, skipped = decisions.scan_for_delivery(
                        tree, role=args.owner, session=session, now=now())
                except Exception:
                    wake, skipped = [], []
                if due or wake:
                    if not cli_alive(): return 0
                    for row in due:
                        row.update(emitted_at=now(), emitted_session=session)
                        registry.save(row)
                    report = registry.report(due) if due else ''
                    if wake:
                        section = decision_report(wake)
                        if skipped:
                            section += ('\n' + '\n'.join(
                                ''.join(c for c in line if c.isprintable())
                                for line in skipped))
                        report = (report.rstrip('\n') + '\n' + section) if report else section
                    try:
                        # UD-C12: the report is written first; only a session
                        # that received it has the delivery recorded.
                        sys.stderr.write(report)
                        sys.stderr.flush()
                    except OSError:
                        return 0
                    for rec in wake:
                        try:
                            decisions.record_delivery(tree, rec['id'], session=session,
                                                      now=now())
                        except decisions.DecisionError:
                            pass    # answered elsewhere between the two locks
                    return 2
                active = [r for r in rows if r['state'] in ('starting', 'running')]
                if not active: return 0
                if now() >= deadline:
                    if not cli_alive(): return 0
                    print(registry.report(active, heartbeat=True), file=sys.stderr, end='')
                    return 2
            time.sleep(0.1)
    finally:
        with registry.lock():
            if read_json(lock) == holder:
                with contextlib.suppress(FileNotFoundError): lock.unlink()


def dispatch(paths, args):
    registry = Registry(paths)
    args.paths = paths
    with registry.lock():
        removed = registry.prune()
    action = args.bg_command
    if action == 'run': return run(registry, args)
    if action == 'watch': return watch(registry, args)
    if action == 'prune':
        print('\n'.join(removed))
        return 0
    if action == 'list':
        with registry.lock(): rows = registry.rows(args.owner)
        if args.json: print(json.dumps(rows))
        else:
            for row in rows:
                print(f"{row['id']} {row.get('name', '')}: {row['state']}" + (' unacked' if row['unacked'] else ''))
        return 0
    if action == 'ack':
        code = 0
        with registry.lock():
            for ident in args.ids:
                row = registry.get(ident)
                if row is None or row['state'] == 'invalid': code = 1
                elif row['state'] in ENDED:
                    if row['acked_at'] is None:
                        row['acked_at'] = now()
                        registry.save(row)
        return code
    if action == 'stop':
        with registry.lock():
            row = registry.get(args.id)
            if row is None or row['state'] == 'invalid': return 1
            if row['state'] in ENDED:
                print(f"{row['id']}: {row['state']}")
                return 0
            pid, started = row['pid'], row['pid_start']
            if not living(pid, started): return 1
            # Only a session/group leader created by bg run is signalable.
            try:
                if os.getpgid(pid) != pid or os.getsid(pid) != pid: return 1
            except ProcessLookupError:
                row = registry.get(args.id)
                if row is not None and row['state'] in ENDED:
                    print(f"{row['id']}: {row['state']}")
                    return 0
                return 1
            with contextlib.suppress(ProcessLookupError): os.killpg(pid, signal.SIGTERM)
            end = time.monotonic() + 10
            while living(pid, started) and time.monotonic() < end: time.sleep(0.05)
            if living(pid, started):
                with contextlib.suppress(ProcessLookupError): os.killpg(pid, signal.SIGKILL)
                while living(pid, started): time.sleep(0.01)
            row.update(state='stopped', ended_at=now())
            registry.save(row)
            print(f"{row['id']}: stopped")
        return 0
    if action == 'wait':
        deadline = None if args.timeout is None else time.monotonic() + args.timeout
        while True:
            with registry.lock():
                row = registry.get(args.id)
                if row is None or row['state'] == 'invalid': return 1
                if row['state'] in ENDED:
                    print(registry.report([row]), file=sys.stderr, end='')
                    if row['acked_at'] is None:
                        row['acked_at'] = now()
                        registry.save(row)
                    return 0 if row['state'] == 'finished' and row['exit_code'] == 0 else 1
            if deadline is not None and time.monotonic() >= deadline: return 124
            time.sleep(0.1)
    return 1


def pending(paths):
    root = state_root() / 'bg' / paths.slug
    if not root.exists(): return []
    registry = Registry(paths)
    with registry.lock():
        return [r for r in registry.rows() if r['state'] in ('starting', 'running') or r['unacked']]


if __name__ == '__main__':
    supervise()

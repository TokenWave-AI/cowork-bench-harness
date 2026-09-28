"""Linux process-group ownership and bounded cleanup for one native invocation."""
import os
from pathlib import Path
import signal
import time


def proc_identity(pid):
    text = (Path('/proc')/str(pid)/'stat').read_text()
    fields = text[text.rfind(')')+2:].split()
    return {'pid': int(pid), 'state': fields[0], 'ppid': int(fields[1]),
            'pgid': int(fields[2]), 'sid': int(fields[3]), 'start_ticks': int(fields[19])}


def capture(process, session_id):
    # Read before poll/wait: even an immediately exited leader remains a zombie,
    # so its original identity is still available until this parent reaps it.
    leader = proc_identity(process.pid)
    if leader['pgid'] != process.pid or leader['sid'] != process.pid:
        raise RuntimeError('Native CLI did not enter its dedicated process session')
    return {'schema': 'claude-owned-process-group-v1', 'leader': leader,
            'driver_pid': os.getpid(), 'session_id': session_id,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'captured_at': time.time()}


def members(ownership):
    leader = ownership['leader']
    if ownership.get('schema') != 'claude-owned-process-group-v1' or leader['pid'] != leader['sid'] or leader['pid'] != leader['pgid']:
        raise RuntimeError('Invalid process-group ownership record')
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != ownership['boot_id']:
        raise RuntimeError('Process-group record belongs to a different machine boot')
    try:
        current = proc_identity(leader['pid'])
    except FileNotFoundError:
        current = None
    if current and (current['start_ticks'] != leader['start_ticks'] or current['sid'] != leader['sid'] or current['pgid'] != leader['pgid']):
        raise RuntimeError('Native leader identity was reused; refuse to signal')
    result = []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            item = proc_identity(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        if item['pgid'] != leader['pgid']:
            continue
        if item['sid'] != leader['sid'] or item['start_ticks'] < leader['start_ticks']:
            raise RuntimeError('Process group has an unexpected session identity')
        if item['state'] != 'Z':
            result.append(item)
    return result


def signal_owned(ownership, sig):
    found = members(ownership)
    if found:
        try:
            os.killpg(ownership['leader']['pgid'], sig)
        except ProcessLookupError:
            pass
    return found


def cleanup(ownership, *, grace_seconds=5, kill_seconds=2):
    before = signal_owned(ownership, signal.SIGTERM)
    deadline = time.monotonic()+grace_seconds
    while members(ownership) and time.monotonic() < deadline:
        time.sleep(0.05)
    killed = signal_owned(ownership, signal.SIGKILL)
    deadline = time.monotonic()+kill_seconds
    while members(ownership) and time.monotonic() < deadline:
        time.sleep(0.05)
    after = members(ownership)
    report = {'passed': not after, 'signalled_pids': [x['pid'] for x in before],
              'killed_pids': [x['pid'] for x in killed], 'surviving_pids': [x['pid'] for x in after],
              'pgid': ownership['leader']['pgid'], 'session_id': ownership['session_id'], 'at': time.time()}
    if after:
        raise RuntimeError('Native process-group cleanup failed: '+str(report))
    return report

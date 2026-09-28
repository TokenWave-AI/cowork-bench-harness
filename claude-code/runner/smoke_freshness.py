"""Bind every preflight and resume to one immutable local job/runtime identity."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

REVISION = 'claude-fresh-smoke-v2'


def write_once(path, value):
    """Write immutable metadata; an existing receipt or binding is never replaced."""
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def smoke_lock(root, row, condition):
    directory = Path(root) / 'preflight'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / f'.smoke-{row:03d}-{condition}.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another smoke for this row/condition is still running')
        yield


def reserve_smoke(root, row, condition):
    root = Path(root)
    if condition not in ('cowork', 'oracle'):
        raise ValueError('Unsupported smoke condition')
    # Keep row and condition as the final two components for agent_env parsing.
    smoke_id = uuid.uuid4().hex
    job_id = f'probe-{smoke_id}-{row:03d}-{condition}'
    paths = [root / 'preflight' / job_id, root / 'sessions' / job_id, root / 'agent-workdirs' / job_id]
    for path in paths:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=False)
    identity = {'revision': REVISION, 'smoke_id': smoke_id, 'job_id': job_id,
                'row': row, 'condition': condition, 'created_at': time.time(),
                'session_dir': str(paths[1]), 'workdir': str(paths[2])}
    write_once(paths[0] / 'SMOKE_IDENTITY.json', identity)
    return paths[0], identity


def bind_run(job_root, state):
    job_root = Path(job_root)
    binding = {'revision': REVISION, 'job_id': job_root.name, 'run_id': state['run_id'],
               'row': state['row'], 'condition': state['kind'], 'deadline': state['deadline']}
    path = job_root / 'RUN_BINDING.json'
    if path.exists():
        if json.loads(path.read_text()) != binding:
            raise RuntimeError('Refusing conversation/job reuse across runtime environments')
    else:
        if (job_root / 'GOAL_STATE.json').exists() or any((job_root / 'attempts').glob('*')):
            raise RuntimeError('Existing conversation has no immutable runtime binding')
        write_once(path, binding)
    return binding


def check_attempt(job_root, state, status, resume):
    job_root = Path(job_root)
    if status['job_id'] != job_root.name:
        raise RuntimeError('Job directory and session identity do not match')
    binding = bind_run(job_root, state)
    if status.get('run_id', state['run_id']) != binding['run_id']:
        raise RuntimeError('Status and runtime identity do not match')
    goal_path = job_root / 'GOAL_STATE.json'
    if not resume and goal_path.exists():
        raise RuntimeError('Fresh attempt refuses an existing conversation')
    if resume:
        if not goal_path.exists() or not json.loads(goal_path.read_text()).get('threadId'):
            raise RuntimeError('Resume needs an existing thread in the same runtime')
    return binding


def freshness_report(job_root, state, identity):
    """Every Claude native turn must remain in this freshly bound smoke session."""
    job_root = Path(job_root)
    problems = []
    goal_path = job_root / 'GOAL_STATE.json'
    goal = json.loads(goal_path.read_text()) if goal_path.exists() else {}
    thread_id = goal.get('threadId')
    expected_workspace = identity['workdir']
    created = goal.get('created_at')
    if (not thread_id or goal.get('workspace') != expected_workspace
            or not state or goal.get('deadline') != state['deadline']
            or not isinstance(created, (int, float)) or created < identity['created_at'] - 1):
        problems.append('goal_identity_or_creation_mismatch')
    drivers = sorted((job_root / 'attempts').glob('*/DRIVER_INVOCATION.json'))
    if len(drivers) != 1:
        problems.append('expected_one_fresh_smoke_driver')
    for path in drivers:
        invocation = json.loads(path.read_text())
        if (invocation.get('resume') is not False or invocation.get('session') != thread_id
                or invocation.get('run_id') != state.get('run_id')
                or invocation.get('original_deadline') != state.get('deadline')
                or invocation.get('workspace') != expected_workspace
                or invocation.get('job') != str(job_root)):
            problems.append('driver_identity_mismatch')
    native = sorted((job_root / 'attempts').glob('*/INVOCATION-*.json'))
    if not native:
        problems.append('missing_native_invocation')
    for index, path in enumerate(native):
        invocation = json.loads(path.read_text())
        argv = invocation.get('argv') or []
        try:
            session_arg = argv[argv.index('--session-id' if index == 0 else '--resume') + 1]
        except (ValueError, IndexError):
            session_arg = None
        if (invocation.get('session_id') != thread_id or session_arg != thread_id
                or invocation.get('deadline') != state.get('deadline')
                or invocation.get('cwd') != expected_workspace
                or not isinstance(invocation.get('started_at'), (int, float))
                or invocation['started_at'] < identity['created_at'] - 1
                or (index == 0 and '--resume' in argv)):
            problems.append('native_invocation_identity_mismatch')
    init_count = 0
    malformed = 0
    earliest = None
    for path in sorted((job_root / 'attempts').glob('*/trajectory.jsonl')):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
                at = event.get('_controller_t')
                if isinstance(at, (int, float)):
                    earliest = at if earliest is None else min(earliest, at)
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    init_count += 1
                    if event.get('session_id') != thread_id:
                        problems.append('trajectory_session_mismatch')
                if event.get('type') == 'result' and event.get('session_id') != thread_id:
                    problems.append('trajectory_result_session_mismatch')
            except (ValueError, AttributeError, TypeError):
                malformed += 1
    if not init_count or malformed or earliest is None or earliest < identity['created_at'] - 1:
        problems.append('missing_or_historical_trajectory')
    return {'revision': REVISION, 'passed': not problems, 'smoke_id': identity['smoke_id'],
            'job_id': identity['job_id'], 'run_id': state['run_id'] if state else None,
            'thread_id': thread_id, 'driver_invocations': len(drivers), 'native_invocations': len(native),
            'init_count': init_count, 'malformed_events': malformed, 'first_event_at': earliest,
            'goal_created_at': created, 'problems': sorted(set(problems))}


def publish_receipt(root, job_root, identity, report):
    root, job_root = Path(root), Path(job_root)
    report.update(smoke_identity=identity, receipt_revision=REVISION)
    receipt = job_root / 'PREFLIGHT.json'
    write_once(receipt, report)
    # Only this small locator is replaceable; every source receipt is immutable.
    index = root / 'preflight' / f"LATEST-{identity['row']:03d}-{identity['condition']}.json"
    pointer = {'revision': REVISION, 'receipt': str(receipt.relative_to(root / 'preflight')),
               'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(), 'smoke_id': identity['smoke_id'],
               'run_id': report.get('run_id'), 'passed': report.get('passed') is True}
    temporary = index.with_name(index.name + '.' + identity['smoke_id'] + '.tmp')
    write_once(temporary, pointer)
    temporary.replace(index)


def load_receipt(preflight_root, row, condition):
    root = Path(preflight_root).resolve()
    pointer = json.loads((root / f'LATEST-{row:03d}-{condition}.json').read_text())
    receipt = root / pointer['receipt']
    if receipt.is_symlink() or root not in receipt.resolve().parents:
        raise RuntimeError('Preflight pointer escaped its root')
    if pointer.get('revision') != REVISION or hashlib.sha256(receipt.read_bytes()).hexdigest() != pointer.get('sha256'):
        raise RuntimeError('Preflight receipt identity/hash mismatch')
    report = json.loads(receipt.read_text())
    identity = report.get('smoke_identity') or {}
    freshness = report.get('freshness') or {}
    binding = json.loads((receipt.parent / 'RUN_BINDING.json').read_text())
    if (report.get('receipt_revision') != REVISION or freshness.get('passed') is not True
            or report.get('row') != row or report.get('condition') != condition
            or identity.get('smoke_id') != pointer.get('smoke_id')
            or identity.get('job_id') != receipt.parent.name
            or freshness.get('job_id') != identity.get('job_id')
            or freshness.get('run_id') != report.get('run_id')
            or binding.get('run_id') != report.get('run_id')
            or pointer.get('run_id') != report.get('run_id')):
        raise RuntimeError('Preflight has missing or mixed smoke/runtime evidence')
    return report

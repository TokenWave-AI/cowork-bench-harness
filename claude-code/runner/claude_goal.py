"""Durable, controller-bound Goal protocol shared by the Claude driver and MCP."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

GOAL_NAMES = frozenset({'create_goal', 'get_goal', 'update_goal'})
GOAL_TOOLS = [
    {'name': 'create_goal', 'description': 'Read the controller-created goal. Its objective and original deadline are immutable.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'get_goal', 'description': 'Read the current task goal, progress, deadline and remaining wall-clock budget.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'update_goal', 'description': 'Record goal progress. Complete only when the full requested functionality and verification are done. A genuine unchanged blocker needs three consecutive completed turns before the controller stops. Temporary provider/tool errors are not final blockers.',
     'inputSchema': {'type': 'object', 'properties': {
         'status': {'type': 'string', 'enum': ['active', 'complete', 'blocked']},
         'summary': {'type': 'string'}, 'blocker': {'type': 'string'}},
         'required': ['status', 'summary'], 'additionalProperties': False}},
]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def transaction(path):
    path = Path(path)
    with path.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        yield state
        state['updated_at'] = time.time()
        save(path, state)


def initialize(path, session, workspace, deadline, objective, fingerprint):
    path = Path(path)
    expected = dict(threadId=session, workspace=str(workspace), deadline=deadline,
                    objective=objective, experiment_fingerprint=fingerprint)
    if path.exists():
        state = json.loads(path.read_text())
        if any(state.get(k) != v for k, v in expected.items()):
            raise ValueError('Cannot resume a different session, workspace, deadline or experiment')
        return state
    state = {'schema': 'claude-cowork-goal-v1', **expected, 'created_at': time.time(),
             'goal': {'status': 'active'}, 'turn_sequence': 0, 'blocked_streak': 0,
             'last_blocker': None, 'stopReason': None, 'history': [], 'native_session_started': False}
    save(path, state)
    return state


def begin_turn(path):
    with transaction(path) as state:
        if state['goal']['status'] in ('complete', 'blocked'):
            raise ValueError('Goal is already terminal')
        state['turn_sequence'] += 1
        state['pending_update'] = None
        return state['turn_sequence']


def call(path, name, arguments, turn):
    with transaction(path) as state:
        if name not in GOAL_NAMES:
            raise ValueError('Unknown goal tool')
        if state['turn_sequence'] != turn:
            raise ValueError('Stale goal tool process')
        if name == 'update_goal':
            status = arguments.get('status')
            summary = arguments.get('summary')
            if status not in ('active', 'complete', 'blocked') or not isinstance(summary, str) or not summary.strip():
                raise ValueError('Goal update needs a valid status and substantive summary')
            blocker = arguments.get('blocker', '').strip()
            if status == 'blocked' and not blocker:
                raise ValueError('Blocked status needs a concrete blocker')
            state['pending_update'] = {'status': status, 'summary': summary, 'blocker': blocker, 'turn': turn}
            state['history'].append({'at': time.time(), **state['pending_update']})
        return public(state)


def public(state):
    return {**{k: state.get(k) for k in ('objective', 'goal', 'pending_update', 'blocked_streak', 'turn_sequence', 'deadline', 'stopReason')},
            'remaining_seconds': max(0, state['deadline'] - time.time()),
            'completion_note': 'Updates commit after a successful Claude turn. Without a terminal goal the controller continues the same session.'}


def finish_turn(path, *, successful):
    with transaction(path) as state:
        update = state.pop('pending_update', None)
        if successful:
            status = (update or {}).get('status', 'active')
            blocker = (update or {}).get('blocker')
            if status == 'blocked':
                state['blocked_streak'] = state['blocked_streak'] + 1 if blocker == state['last_blocker'] else 1
                state['last_blocker'] = blocker
            else:
                state['blocked_streak'] = 0
                state['last_blocker'] = None
            state['goal'] = {'status': ('blocked' if state['blocked_streak'] >= 3 else 'active') if status == 'blocked' else status,
                             'summary': (update or {}).get('summary'), 'blocker': blocker}
            state['stopReason'] = {'complete': 'goal-complete', 'blocked': 'goal-blocked'}.get(state['goal']['status'])
        return dict(state)

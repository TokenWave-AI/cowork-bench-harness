"""Claude Code goal loop: same session/workspace/deadline through every turn."""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from claude_events import model_metrics, registry_valid, success
from claude_goal import begin_turn, finish_turn, initialize, save, transaction
from claude_process_group import capture, cleanup
from claude_tool_guard import ToolIsolationError, UnregisteredToolGuard
from preflight_binding import bound_run_condition, expected_tool_names
from claude_native_user import MCPRelay, native_environment, drop_identity, write_native_config

STOP = threading.Event()

GOAL_INSTRUCTIONS = '''
The controller has already created your task Goal. Use the cowork get_goal tool
to inspect it and update_goal to record progress. Keep it active while anything
remains. A normal final message without a complete Goal starts another turn.
Use update_goal(status="complete", summary=...) only after all available explicit
unfinished requirements have been implemented and meaningfully verified. Check
relevant documents, chats, board cards, reviews, artifacts and repository evidence;
resolve version conflicts and follow up outstanding questions and late replies.
For a benchmark goal record requirements, implementations, verification and exact
blockers in /app/SOLUTION.md through the task terminal. A connectivity-only goal
requires only its explicitly requested tool checks and a complete Goal update;
do not edit task files or attempt the full benchmark during that check.
Do not replace production
implementation with shallow assertions, hard-coded answers or prose.
For a genuine blocker use update_goal(status="blocked", blocker=<same precise
reason>, summary=...) and investigate alternatives in the next turn. The same
blocker must persist for three consecutive completed turns before stopping.
Temporary colleague delay, recoverable API/tool failure, partial work or elapsed
time alone are not final blockers. Never discard existing work on continuation.
The original eight-hour wall-clock budget includes waits and retries. Complete
work may finish early. The budget cannot be reset by any tool or continuation.
'''


def goal_instructions(condition):
    expected_tool_names(condition, 1)
    if condition == 'oracle':
        return GOAL_INSTRUCTIONS.replace(
            'Check\nrelevant documents, chats, board cards, reviews, artifacts and repository evidence;\nresolve version conflicts and follow up outstanding questions and late replies.',
            'Use the complete task specification and repository evidence to implement and verify every requirement.').replace(
            'Temporary colleague delay, recoverable API/tool failure, partial work or elapsed',
            'Recoverable API/tool failure, partial work or elapsed')
    return GOAL_INSTRUCTIONS


def command(cfg, mcp_path, session, resume, system, condition):
    return [cfg['claude_binary'], '--bare', '-p', '--model', cfg['model'], '--effort', cfg['effort'],
            '--tools', '', '--strict-mcp-config', '--mcp-config', str(mcp_path),
            '--allowedTools', 'mcp__cowork__*', '--allow-dangerously-skip-permissions', '--permission-mode', 'bypassPermissions',
            '--setting-sources', '', '--disable-slash-commands', '--no-chrome',
            '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
            '--replay-user-messages', '--system-prompt', system + goal_instructions(condition),
            '--resume' if resume else '--session-id', session]


def terminate(process, ownership):
    result = cleanup(ownership)
    # poll() may already have reaped the group leader. The group itself was
    # still inspected above, so live MCP grandchildren cannot escape cleanup.
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Native process leader could not be reaped')
    return result


def run_native(argv, prompt, workdir, trajectory, stderr_path, deadline, session, cfg, state_path=None, *, row, condition):
    secret = os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY', '')
    def clean(text):
        return text.replace(secret, '[REDACTED]') if secret else text
    process = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True, bufsize=1,
                               env=native_environment(cfg, workdir), **drop_identity(cfg))
    ownership = capture(process, session)
    ownership_path = trajectory.parent/f'NATIVE_PROCESS-{process.pid}.json'
    try:
        save(ownership_path, ownership)
    except BaseException:
        terminate(process, ownership)
        raise
    drained_at = None
    cleanup_report = None
    def stop_native():
        nonlocal cleanup_report, drained_at
        if cleanup_report is None:
            cleanup_report = terminate(process, ownership)
            save(ownership_path.with_name(ownership_path.stem+'-CLEANUP.json'), cleanup_report)
            drained_at = time.monotonic()
    messages = queue.Queue()
    def read(stream, label):
        try:
            for line in stream:
                messages.put((label, line))
        finally:
            messages.put((label, None))
    for stream, label in [(process.stdout, 'stdout'), (process.stderr, 'stderr')]:
        threading.Thread(target=read, args=(stream, label), daemon=True).start()
    try:
        process.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt}}) + '\n')
        process.stdin.close()
    except BrokenPipeError:
        # Preserve the process' structured error even if it exits before stdin.
        pass
    init, result, reason, closed, seen_tools, pending = None, None, 'process_exit', set(), set(), set()
    unregistered = UnregisteredToolGuard(expected_tool_names(condition, row), session)
    isolation_failure = None
    last_event = time.time()
    with trajectory.open('a') as out, stderr_path.open('a') as err:
        try:
            while len(closed) < 2 or process.poll() is None:
                now = time.time()
                if STOP.is_set():
                    reason = 'operator_stop'; stop_native()
                elif now >= deadline:
                    reason = 'time_budget'; stop_native()
                elif not pending and now-last_event >= cfg.get('transport_idle_restart_seconds', 600):
                    reason = 'transport_stall'; stop_native()
                elif process.poll() is not None:
                    stop_native()
                try:
                    unregistered.check_timeout(now)
                except ToolIsolationError as error:
                    isolation_failure = isolation_failure or str(error)
                    reason = 'registry_error'; stop_native()
                # Pipe EOF is not a deadline prerequisite. A child holding a
                # descriptor cannot keep this driver alive after bounded cleanup.
                if drained_at is not None and time.monotonic()-drained_at >= 1:
                    break
                try:
                    label, line = messages.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    closed.add(label)
                    continue
                if label == 'stderr':
                    err.write(clean(line)); err.flush()
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('event not an object')
                except ValueError:
                    err.write('Malformed Claude stream event: ' + clean(line[:1000])); err.flush()
                    reason = 'protocol_error'; stop_native()
                    continue
                event['_controller_t'] = now
                out.write(clean(json.dumps(event, ensure_ascii=False)) + '\n'); out.flush()
                last_event = now
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    init = event
                    if event.get('session_id') != session or not registry_valid(init, cfg['model'], row, condition):
                        isolation_failure = isolation_failure or 'init_registry_or_session_mismatch'
                        reason = 'registry_error'; stop_native()
                    elif state_path is not None:
                        with transaction(state_path) as durable:
                            durable['native_session_started'] = True
                if event.get('type') == 'result':
                    result = event
                    try:
                        unregistered.require_settled()
                    except ToolIsolationError as error:
                        isolation_failure = isolation_failure or str(error)
                        reason = 'registry_error'; stop_native()
                message = event.get('message') or {}
                parts = message.get('content') if isinstance(message, dict) else []
                for part in parts if isinstance(parts, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if event.get('type') == 'assistant' and part.get('type') == 'tool_use':
                        pending.add(part.get('id')); seen_tools.add(part.get('name'))
                        try:
                            if (not registry_valid(init, cfg['model'], row, condition)
                                    or event.get('session_id') != session):
                                raise ToolIsolationError('tool_call_without_bound_registry')
                            unregistered.tool_call(part, now)
                        except ToolIsolationError as error:
                            isolation_failure = isolation_failure or str(error)
                            reason = 'registry_error'; stop_native()
                    if event.get('type') == 'user' and part.get('type') == 'tool_result':
                        try:
                            unregistered.tool_result(event, part, now)
                        except ToolIsolationError as error:
                            isolation_failure = isolation_failure or str(error)
                            reason = 'registry_error'; stop_native()
                        pending.discard(part.get('tool_use_id'))
        finally:
            stop_native()
            # Do not block closing a file still owned by a reader thread.
            if 'stdout' in closed:
                process.stdout.close()
            if 'stderr' in closed:
                process.stderr.close()
    try:
        unregistered.require_settled()
    except ToolIsolationError as error:
        isolation_failure = isolation_failure or str(error)
        if reason == 'process_exit':
            reason = 'registry_error'
    passed = reason == 'process_exit' and success(result, init, process.returncode, cfg['model'], session, row, condition)
    return {'exit_code': process.returncode, 'reason': reason, 'success': passed, 'native_result': result,
            'row': row, 'condition': condition, 'init_valid': registry_valid(init, cfg['model'], row, condition),
            'tools_seen': sorted(x for x in seen_tools if x),
            'unregistered_tool_rejections': unregistered.rejected,
            'unregistered_tool_calls_pending': sorted(unregistered.pending),
            'isolation_failure': isolation_failure}


def drive(cfg, *, job, state, session, workspace, row, condition, deadline, original_deadline,
          fingerprint, objective, prompt, system, run_id, target, resume=False):
    if type(row) is not int or row not in {entry['row'] for entry in cfg['tasks']}:
        raise ValueError('Driver row is outside this immutable campaign shard')
    if condition not in cfg['conditions']:
        raise ValueError('Driver condition is disabled')
    bound_run_condition(job.parents[1]/'runs', run_id, row, condition)
    initialize(state, session, workspace, original_deadline, objective, fingerprint)
    last = None
    while time.time() < deadline and not STOP.is_set():
        goal = json.loads(state.read_text())
        if goal['goal']['status'] in ('complete', 'blocked'):
            return {'is_error': False, 'terminal_reason': 'completed' if goal['goal']['status'] == 'complete' else 'goal-blocked', 'goal': goal}
        turn = begin_turn(state)
        config = {'mcpServers': {'cowork': {'command': sys.executable,
            'args': ['-B', str(Path(__file__).with_name('claude_goal_mcp.py')), '--state', str(state),
                     '--run-id', run_id, '--row', str(row), '--condition', condition, '--turn', str(turn)],
            'env': {'DOCKER_HOST': cfg['docker_host'], 'COWORK_RUNS': str(job.parents[1]/'runs'),
                    'TMPDIR': str(job.parents[1]/'tmp'), 'PYTHONDONTWRITEBYTECODE': '1'}}}}
        backend = config['mcpServers']['cowork']
        backend_env = {k: v for k, v in os.environ.items()
                       if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'OPENAI_', 'TOKENWAVE_'))
                       and not k.endswith(('API_KEY', 'AUTH_TOKEN'))}
        backend_env.update(backend.get('env', {}))
        relay = MCPRelay(cfg, [backend['command'], *backend['args']], backend_env)
        config['mcpServers']['cowork'] = {'command': '/usr/bin/python3',
            'args': ['-B', str(Path(__file__).with_name('claude_mcp_socket.py')), str(relay.path)]}
        native_environment(cfg, workspace)
        mcp_path = workspace / f'.mcp-turn-{turn:05d}.json'
        write_native_config(mcp_path, config, cfg)
        # An error before Claude's first init creates no resumable native session.
        # Reuse --session-id then; after init always use the same --resume ID.
        native_resume = bool(goal.get('native_session_started'))
        argv = command(cfg, mcp_path, session, native_resume, system, condition)
        save(target / f'INVOCATION-{turn:05d}.json', {'argv': argv, 'cwd': str(workspace),
             'session_id': session, 'turn': turn, 'deadline': original_deadline,
             'row': row, 'condition': condition,
             'credentials_logged': False, 'started_at': time.time()})
        with relay:
            last = run_native(argv, prompt, workspace, target/'trajectory.jsonl', target/'stderr.log', deadline, session, cfg, state, row=row, condition=condition)
        save(target / f'MCP_RELAY-{turn:05d}.json', {'native_user': cfg['native_solver_user'],
            'socket_removed': not relay.path.exists(), 'errors': relay.errors,
            'child_reaped': relay.child is None or relay.child.poll() is not None})
        save(target / f'TURN-{turn:05d}.json', last)
        goal = finish_turn(state, successful=last['success'])
        if not last['success']:
            reason = last['reason']
            error = last.get('native_result') or {'error': 'API Error: stream ended before valid result'}
            if reason in ('registry_error', 'protocol_error'):
                error = {'error': 'Invalid Claude protocol or isolated tool registry'}
            elif reason == 'transport_stall':
                error = {'error': 'API Error: connection timed out without output or pending tool'}
            return {'is_error': True, 'terminal_reason': reason, 'error': error, 'goal': goal,
                    'permission_denials': (last.get('native_result') or {}).get('permission_denials', [])}
        if goal['goal']['status'] in ('complete', 'blocked'):
            return {'is_error': False, 'terminal_reason': 'completed' if goal['goal']['status'] == 'complete' else 'goal-blocked', 'goal': goal}
        resume = True
        prompt = ('Your Goal remains active. Continue the same repository task using your saved work and requirements. '
                  'Audit what remains; implement and validate it. Resolve pending questions and genuine blockers. '
                  'When the full functional goal is verified, use update_goal complete. The original deadline is unchanged.')
    return {'is_error': False, 'terminal_reason': 'operator_stop' if STOP.is_set() else 'time_budget',
            'goal': json.loads(state.read_text())}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--invocation', type=Path, required=True)
    args = p.parse_args()
    invocation = json.loads(args.invocation.read_text())
    cfg = invocation.pop('config')
    for key in ('job', 'state', 'workspace', 'target'):
        invocation[key] = Path(invocation[key])
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    result = drive(cfg, **invocation)
    save(invocation['target']/'DRIVER_RESULT.json', result)
    return 1 if result['is_error'] else 0


if __name__ == '__main__':
    sys.exit(main())

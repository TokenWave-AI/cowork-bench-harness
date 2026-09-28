"""Claude Code implementation of the shared campaign runner adapter interface."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from claude_events import model_metrics
from claude_goal import initialize, save
from preflight_binding import expected_tool_names, fingerprint
from inspect_claude_request import Proxy
from smoke_freshness import bind_run, check_attempt
from claude_process_group import cleanup

C = None


def agent_env(job_id):
    # Authentication is supplied only to the solver CLI; configurations contain paths.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'OPENAI_', 'CODEX_', 'TOKENWAVE_', 'COLLEAGUE_'))
           and not k.endswith(('API_KEY', 'AUTH_TOKEN'))}
    env.update(ANTHROPIC_AUTH_TOKEN=C.KEY, ANTHROPIC_BASE_URL=C.CFG['provider_base_url'],
               CLAUDE_CONFIG_DIR=str(C.ROOT/'sessions'/job_id),
               CLAUDE_CODE_EFFORT_LEVEL=C.CFG['effort'],
               ANTHROPIC_DEFAULT_OPUS_MODEL=C.CFG['model'], ANTHROPIC_DEFAULT_SONNET_MODEL=C.CFG['model'],
               ANTHROPIC_DEFAULT_HAIKU_MODEL=C.CFG['model'],
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1',
               ENABLE_TOOL_SEARCH='false', MCP_TIMEOUT='120000', MCP_TOOL_TIMEOUT='3660000',
               TMPDIR=str(C.ROOT/'tmp'), XDG_CACHE_HOME=str(C.ROOT/'cache'), PYTHONDONTWRITEBYTECODE='1')
    return env


def configure_mcp(job_root, state):
    bind_run(job_root, state)
    (C.ROOT/'sessions'/job_root.name).mkdir(mode=0o700, parents=True, exist_ok=True)
    save(job_root/'CLAUDE_CONFIG.json', {'harness': 'claude-code-goal-proxy-v1',
        'row': state['row'], 'condition': state['kind'],
        'expected_business_tools': len(expected_tool_names(state['kind'], state['row'])) - 3,
        'expected_goal_tools': 3, 'credentials_in_config': False,
        'run_id': state['run_id'], 'model': C.CFG['model'], 'effort': C.CFG['effort']})


def attempt(job_root, state, status, number, session_id, prompt, resume=False, limit=None):
    check_attempt(job_root, state, status, resume)
    condition = state['kind']
    if status.get('condition') != condition or condition not in C.CFG['conditions']:
        raise ValueError('Attempt condition differs from native run')
    workdir = C.ROOT/'agent-workdirs'/status['job_id']; workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = job_root/'attempts'/f'{number:03d}'; target.mkdir(mode=0o700, parents=True, exist_ok=False)
    goal_path = job_root/'GOAL_STATE.json'
    objective = ('Complete only the explicitly requested tool connectivity check. Do not implement the benchmark task.'
                 if limit else C.SYSTEM)
    deadline = min(state['deadline'], time.time()+limit) if limit else state['deadline']
    initialize(goal_path, session_id, workdir, state['deadline'], objective, fingerprint(C.CFG))
    invocation = {'config': C.CFG, 'job': str(job_root), 'state': str(goal_path), 'session': session_id,
                  'row': state['row'], 'condition': condition,
                  'workspace': str(workdir), 'deadline': deadline, 'original_deadline': state['deadline'],
                  'fingerprint': fingerprint(C.CFG), 'objective': objective, 'prompt': prompt,
                  'system': objective if limit else C.SYSTEM, 'run_id': state['run_id'], 'target': str(target), 'resume': resume}
    save(target/'DRIVER_INVOCATION.json', invocation)
    argv = [sys.executable, '-B', str(C.ROOT/'scripts/claude_driver.py'), '--invocation', str(target/'DRIVER_INVOCATION.json')]
    started, reason = time.time(), 'process_exit'
    with Proxy(C.CFG, target/'OUTBOUND_REQUESTS.json', row=state['row'], condition=condition) as proxy_url:
        env = agent_env(status['job_id']); env['ANTHROPIC_BASE_URL'] = proxy_url
        with (target/'driver.log').open('w') as out, (target/'stderr.log').open('a') as err:
            process = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=True)
            status.update(agent_pid=process.pid, attempt=number, phase='solving', current_attempt=str(target))
            C.save(job_root/'STATUS.json', status)
            last_update = 0
            try:
                while process.poll() is None:
                    now = time.time()
                    if C.STOP.is_set() or C.control().get('stop'):
                        reason = 'operator_stop'; C.terminate(process); break
                    if now >= deadline:
                        reason = 'time_budget'; C.terminate(process); break
                    if now-last_update >= 15:
                        metrics = model_metrics(sorted((job_root/'attempts').glob('*/trajectory.jsonl')))
                        idle = 0 if metrics['pending_tool_calls'] else max(0, now-(metrics.get('last_event_at') or started))
                        waiting = not metrics['pending_tool_calls'] and (metrics['provider_waiting'] or idle >= 180)
                        status.update(updated_at=now, agent_elapsed_seconds=now-status['agent_started_at'],
                            phase='provider_wait' if waiting else 'solving', tool_calls=metrics['tool_calls'],
                            assistant_messages=metrics['assistant_messages'], native_api_retry_counts=metrics['native_api_retry_counts'],
                            provider_errors=metrics['provider_errors'][-2:], provider_waiting=waiting,
                            transport_idle_seconds=idle, pending_tool_calls=metrics['pending_tool_calls'],
                            last_event_at=metrics['last_event_at'], last_model_or_tool_progress_at=metrics['last_model_or_tool_progress_at'])
                        C.save(job_root/'STATUS.json', status); last_update = now
                    time.sleep(1)
            finally:
                C.terminate(process)
                # The native CLI has its own process session. If the driver
                # crashes or needs SIGKILL, reclaim only its recorded groups.
                for path in target.glob('NATIVE_PROCESS-*.json'):
                    if path.name.endswith('-CLEANUP.json'):
                        continue
                    ownership = json.loads(path.read_text())
                    if ownership.get('driver_pid') != process.pid or ownership.get('session_id') != session_id:
                        raise RuntimeError('Unexpected native process ownership receipt')
                    C.save(path.with_name(path.stem+'-ADAPTER-CLEANUP.json'), cleanup(ownership))
    metrics = model_metrics(sorted((job_root/'attempts').glob('*/trajectory.jsonl')))
    output = target/'DRIVER_RESULT.json'
    final = json.loads(output.read_text()) if output.exists() else {'is_error': True,
        'terminal_reason': reason, 'error': 'API Error: driver ended without durable result'}
    reason = final['terminal_reason'] if final['terminal_reason'] in ('operator_stop', 'time_budget') else reason
    report = {'exit_code': process.returncode, 'reason': reason, 'elapsed_seconds': time.time()-started,
              'result': final, 'tool_calls': metrics['tool_calls'], 'permission_denials': final.get('permission_denials', [])}
    all_requests = []
    for path in sorted((job_root/'attempts').glob('*/OUTBOUND_REQUESTS.json')):
        all_requests.extend(json.loads(path.read_text()))
    C.save(job_root/'OUTBOUND_REQUESTS.json', all_requests)
    C.save(target/'RESULT.json', report)
    return report, metrics


def install(campaign):
    global C
    C = campaign
    for name in ('agent_env', 'configure_mcp', 'model_metrics', 'attempt'):
        setattr(C, name, globals()[name])

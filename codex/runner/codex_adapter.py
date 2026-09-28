"""Connect the shared campaign lifecycle to the existing Codex Goal driver."""
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from preflight_binding import registry_summary, tool_result_success, expected_tool_names
from smoke_freshness import bind_run, check_attempt

C = None


def agent_env(job_id):
    env = os.environ.copy()
    for name in list(env):
        if (name.startswith(('ANTHROPIC_', 'CLAUDE_', 'OPENAI_', 'CODEX_', 'TOKENWAVE_', 'COLLEAGUE_'))
                or name.endswith(('API_KEY', 'AUTH_TOKEN'))):
            env.pop(name, None)
    env.update(TOKENWAVE_API_KEY=C.KEY,
               CODEX_HOME=str(C.ROOT / 'sessions' / job_id),
               TMPDIR=str(C.ROOT / 'tmp'), XDG_CACHE_HOME=str(C.ROOT / 'cache'),
               PYTHONDONTWRITEBYTECODE='1',
               COWORK_EXPECTED_TOOLS=str(len(expected_tool_names(job_id.split('-')[-1], int(job_id.split('-')[-2])))),
               COWORK_EXPECTED_TOOL_NAMES=json.dumps(sorted(expected_tool_names(job_id.split('-')[-1], int(job_id.split('-')[-2])))),
               CODEX_CODE_MODE_HOST=C.CFG['code_mode_host_binary'],
               PATH=str(Path(C.CFG['codex_binary']).parent) + os.pathsep + env.get('PATH', ''))
    return env


def configure_mcp(job_root, state):
    bind_run(job_root, state)
    config_dir = C.ROOT / 'sessions' / job_root.name
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    values = {
        'model': C.CFG['model'], 'model_reasoning_effort': C.CFG['effort'],
        'model_provider': 'tokenwave', 'approval_policy': 'never', 'sandbox_mode': 'danger-full-access',
        'web_search': 'disabled', 'project_doc_max_bytes': 0,
        'developer_instructions': C.SYSTEM,
    }
    text = ''.join(f'{k} = {json.dumps(v)}\n' for k, v in values.items())
    disabled = ['shell_tool', 'unified_exec', 'view_image', 'multi_agent', 'multi_agent_v2',
                'apps', 'browser_use', 'browser_use_external', 'computer_use', 'image_generation',
                'artifact', 'workspace_dependencies', 'plugins', 'remote_plugin', 'hooks', 'memories',
                'skill_search', 'code_mode', 'code_mode_only', 'sleep_tool',
                'enable_request_compression', 'unbounded_connection_retries']
    text += '\n[features]\n' + ''.join(f'{k} = false\n' for k in disabled)
    text += 'goals = true\ncode_mode_host = true\nskip_host_skill_discovery = true\n'
    provider = {'name': C.CFG.get('provider_display_name', 'Tokenwave'), 'base_url': C.CFG['provider_base_url'],
                'env_key': 'TOKENWAVE_API_KEY', 'wire_api': 'responses',
                'supports_websockets': False, 'request_max_retries': 20, 'stream_max_retries': 20,
                'stream_idle_timeout_ms': 300000}
    text += '\n[model_providers.tokenwave]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in provider.items())
    mcp = {'command': sys.executable,
           'args': ['-B', str(C.ROOT / 'scripts/runtime_cli.py'), 'mcp', state['run_id']],
           'required': True, 'startup_timeout_sec': 120, 'tool_timeout_sec': 3660}
    text += '\n[mcp_servers.cowork]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in mcp.items())
    mcp_env = {'DOCKER_HOST': C.CFG['docker_host'], 'COWORK_RUNS': str(C.ROOT / 'runs'),
               'TMPDIR': str(C.ROOT / 'tmp'), 'PYTHONDONTWRITEBYTECODE': '1'}
    text += '\n[mcp_servers.cowork.env]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in mcp_env.items())
    (config_dir / 'config.toml').write_text(text)
    (config_dir / 'config.toml').chmod(0o600)
    C.save(job_root / 'CODEX_CONFIG.json', {'config_file': str(config_dir / 'config.toml'),
                                          'expected_task_tools': len(expected_tool_names(state['kind'], state['row'])),
                                          'credentials_in_config': False})


def model_metrics(paths):
    calls, messages, retries, successful = {}, set(), Counter(), set()
    last_usage, errors, last_event, registry, last_at, progress_at = {}, [], None, {}, None, 0
    pending_tools = set()
    requests = {}
    for path in paths:
        if not path.exists():
            continue
        # A new app-server attempt cannot have live tools from a prior process.
        pending_tools.clear()
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                frame = event.get('frame', {})
                method = frame.get('method')
                params = frame.get('params') or {}
                last_event = method or last_event
                last_at = event.get('t', last_at)
                if event.get('dir') == 'out' and 'id' in frame:
                    requests[(str(path), frame['id'])] = method
                if event.get('dir') != 'in':
                    continue
                if requests.get((str(path), frame.get('id'))) == 'mcpServerStatus/list':
                    registry = frame.get('result') or registry
                if method in ('item/started', 'item/completed'):
                    item = params.get('item') or {}
                    if item.get('type') == 'mcpToolCall':
                        tool = item.get('tool') or item.get('name') or 'unknown'
                        name = tool if tool.startswith('mcp__') else f"mcp__{item.get('server', 'cowork')}__{tool}"
                        calls[item['id']] = name
                        if method == 'item/started':
                            pending_tools.add(item['id'])
                        else:
                            pending_tools.discard(item['id'])
                        if method == 'item/completed' and tool_result_success(item):
                            successful.add(name)
                            progress_at = event.get('t') or progress_at
                    elif item.get('type') == 'agentMessage':
                        messages.add(item['id'])
                        progress_at = event.get('t') or progress_at
                elif method == 'thread/tokenUsage/updated':
                    usage = params.get('tokenUsage') or {}
                    last_usage = usage.get('total') or usage
                elif method == 'error':
                    errors.append({'at': event.get('t'), **params})
                    if params.get('willRetry'):
                        error = params.get('error') or {}
                        info = error.get('codexErrorInfo') or {}
                        retries[json.dumps(info, sort_keys=True) or 'transport'] += 1
    init = registry_summary(registry, C.CFG['model'])
    tools = Counter(calls.values())
    return {'assistant_messages': len(messages), 'tool_calls': len(calls), 'tool_calls_by_name': dict(tools),
            'successful_tools': sorted(successful),
            'observed_message_usage': last_usage, 'native_api_retry_counts': dict(retries),
            'provider_errors': errors[-20:], 'last_event_type': last_event, 'last_event_at': last_at,
            'provider_waiting': bool(errors and (errors[-1].get('at') or 0) > progress_at),
            'pending_tool_calls': len(pending_tools),
            'last_model_or_tool_progress_at': progress_at,
            'init': init, 'result': None}


def transport_idle_seconds(metrics, started, now):
    if metrics.get('pending_tool_calls'):
        return 0.0
    return max(0.0, now - max(started, metrics.get('last_event_at') or started))


def attempt(job_root, state, status, number, session_id, prompt, resume=False, limit=None):
    check_attempt(job_root, state, status, resume)
    workdir = C.ROOT / 'agent-workdirs' / status['job_id']
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = job_root / 'attempts' / f'{number:03d}'
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    instruction = target / 'INSTRUCTION.md'
    instruction.write_text(prompt)
    objective = target / 'OBJECTIVE.md'
    objective.write_text(('Complete only this tool connectivity check, including all explicitly requested tool calls, '
                          'then mark the goal complete and report the evidence. Do not implement the benchmark task.'
                         if limit else C.SYSTEM))
    deadline = min(state['deadline'], time.time() + limit) if limit else state['deadline']
    proxy = None
    if limit:
        from inspect_codex_request import Handler, ThreadingHTTPServer, records
        records.clear()
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        config_path = C.ROOT / 'sessions' / status['job_id'] / 'config.toml'
        original_config = config_path.read_text()
        config_path.write_text(original_config.replace(C.CFG['provider_base_url'], f'http://127.0.0.1:{proxy.server_port}/v1'))
    argv = [sys.executable, '-B', str(C.ROOT / 'scripts/goal_driver.py'),
            '--codex-bin', C.CFG['codex_binary'],
            '--model', C.CFG['model'], '--effort', C.CFG['effort'], '--cwd', str(workdir),
            '--instruction-file', str(instruction), '--objective-file', str(objective),
            '--events-path', str(target / 'trajectory.jsonl'), '--state-path', str(job_root / 'GOAL_STATE.json'),
            '--deadline-seconds', str(max(1, deadline-time.time()-3)), '--rpc-timeout', '120',
            '--rate-limit-max-retries', '20']
    if resume:
        argv.append('--resume')
    C.save(target / 'INVOCATION.json', {'argv': argv, 'cwd': str(workdir), 'resume': resume,
                                      'model': C.CFG['model'], 'effort': C.CFG['effort'],
                                      'provider_base_url': C.CFG['provider_base_url'],
                                      'colleague_base_url': C.CFG['colleague_base_url'],
                                      'credential_file': C.CFG['credential_file'],
                                      'credentials_logged': False, 'started_at': time.time()})
    started, reason = time.time(), 'process_exit'
    with (target / 'driver.log').open('w') as stdout, (target / 'stderr.log').open('w') as stderr:
        process = subprocess.Popen(argv, cwd=workdir, env=agent_env(status['job_id']),
                                   stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        status.update(agent_pid=process.pid, attempt=number, phase='solving', current_attempt=str(target))
        C.save(job_root / 'STATUS.json', status)
        last_update = 0
        try:
            while process.poll() is None:
                now = time.time()
                if C.STOP.is_set() or C.control().get('stop'):
                    reason = 'operator_stop'
                    C.terminate(process)
                    break
                if now >= deadline:
                    reason = 'time_budget'
                    C.terminate(process)
                    break
                if now - last_update >= 15:
                    metrics = model_metrics(sorted((job_root / 'attempts').glob('*/trajectory.jsonl')))
                    idle = transport_idle_seconds(metrics, started, now)
                    waiting = not metrics['pending_tool_calls'] and (metrics['provider_waiting'] or idle >= 180)
                    status.update(updated_at=now, agent_elapsed_seconds=now-status['agent_started_at'],
                                  phase='provider_wait' if waiting else 'solving',
                                  tool_calls=metrics['tool_calls'], assistant_messages=metrics['assistant_messages'],
                                  native_api_retry_counts=metrics['native_api_retry_counts'],
                                  provider_errors=metrics['provider_errors'][-2:],
                                  provider_waiting=waiting, transport_idle_seconds=idle,
                                  pending_tool_calls=metrics['pending_tool_calls'],
                                  last_event_at=metrics['last_event_at'],
                                  last_model_or_tool_progress_at=metrics['last_model_or_tool_progress_at'])
                    C.save(job_root / 'STATUS.json', status)
                    if not limit and idle >= C.CFG.get('transport_idle_restart_seconds', 600):
                        reason = 'transport_stall'
                        status['transport_stall_restarts'] = status.get('transport_stall_restarts', 0) + 1
                        C.save(target / 'TRANSPORT_STALL.json', {'at': now, 'idle_seconds': idle,
                               'pending_tool_calls': 0, 'deadline': deadline,
                               'action': 'interrupt app-server and resume same thread within original deadline'})
                        C.terminate(process)
                        break
                    last_update = now
                time.sleep(1)
        finally:
            C.terminate(process)
            if proxy:
                proxy.shutdown()
                proxy.server_close()
                config_path.write_text(original_config)
                C.save(job_root / 'OUTBOUND_REQUESTS.json', records)
    metrics = model_metrics(sorted((job_root / 'attempts').glob('*/trajectory.jsonl')))
    goal_path = job_root / 'GOAL_STATE.json'
    goal = json.loads(goal_path.read_text()) if goal_path.exists() else {}
    status['session_id'] = goal.get('threadId', status.get('session_id'))
    ending = goal.get('stopReason')
    if ending == 'deadline':
        reason = 'time_budget'
    denials = ([{'unexpected_approval_requests': goal['approvalsAnswered']}]
               if goal.get('approvalsAnswered', 0) else [])
    final = {'is_error': process.returncode != 0, 'terminal_reason': 'completed' if ending == 'goal-complete' else ending,
             'goal': goal.get('goal'), 'errors': goal.get('errors'), 'permission_denials': denials,
             'session_id': goal.get('threadId')}
    if reason == 'transport_stall':
        final.update(is_error=True, terminal_reason='transport_stall',
                     transport_error='API Error: connection timed out without model output or pending tools')
    report = {'exit_code': process.returncode, 'reason': reason, 'elapsed_seconds': time.time()-started,
              'result': final, 'tool_calls': metrics['tool_calls'], 'permission_denials': denials}
    C.save(target / 'RESULT.json', report)
    return report, metrics


def install(campaign):
    global C
    C = campaign
    C.SYSTEM += ('\nFor CoWork, discover and complete all explicit unfinished requirements available in the task workplace, '
                 'including relevant documents, chat, board, reviews and artifacts. Resolve conflicting versions with '
                 'current authoritative evidence and follow up on outstanding questions and late replies. '
                 'For Oracle, the complete specification is provided in the initial instruction; implement it through '
                 'the repository terminal. Keep the goal active until the full requested functionality and meaningful '
                 'validation are complete. Mark a genuine blocker precisely; an isolated temporary API or tool failure '
                 'does not close the goal. Record requirements, implementation and verification in /app/SOLUTION.md.\n')
    for name in ['agent_env', 'configure_mcp', 'model_metrics', 'attempt']:
        setattr(C, name, globals()[name])

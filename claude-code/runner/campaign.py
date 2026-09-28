#!/usr/bin/env python3
"""Codex Goal Oracle/CoWork evaluation with isolated tools and durable retries."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from preflight_binding import eligible_entries, expected_tool_names, fingerprint, outbound_report, outbound_passed, valid_receipt
from smoke_freshness import smoke_lock, reserve_smoke, freshness_report, publish_receipt, load_receipt

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / 'CONFIG.json').read_text())
# The controller imports no engine and reads no credentials. Every task runs in
# a separate process, selecting its verified runtime before any runtime import.
from row_runtime import load_runtime, verify_sources
from process_slots import ProcessSlots
KEY = COLLEAGUE_KEY = ''
engine = None
SELECTED_RUNTIME = None
FROZEN_MANIFEST = json.loads((ROOT / 'MANIFEST.snapshot.json').read_text())
if CFG.get('harness') != 'claude-code':
    raise ValueError('This candidate requires harness=claude-code')
os.environ.update(DOCKER_HOST=CFG['docker_host'], COWORK_RUNS=str(ROOT / 'runs'),
                  TMPDIR=str(ROOT / 'tmp'), XDG_CACHE_HOME=str(ROOT / 'cache'),
                  PYTHONDONTWRITEBYTECODE='1', COWORK_CPUS='2', COWORK_MEMORY='12g')
STOP = threading.Event()
SLOT_ROOT = Path(CFG.get('process_slot_root', str(ROOT / 'process-slots')))
PREPARE_SLOTS = ProcessSlots(SLOT_ROOT, 'prepare', CFG.get('prepare_concurrency', 2),
                             cancelled=lambda: STOP.is_set() or control().get('stop', False))
GRADE_SLOTS = ProcessSlots(SLOT_ROOT, 'grading', CFG.get('grading_concurrency', 2))


def initialize_worker(row, condition, *, credentials=True):
    global engine, SELECTED_RUNTIME, KEY, COLLEAGUE_KEY
    engine, SELECTED_RUNTIME = load_runtime(ROOT, CFG, row, condition)
    if not credentials:
        return
    KEY = Path(CFG['credential_file']).read_text().strip()
    COLLEAGUE_KEY = Path(CFG.get('colleague_credential_file', CFG['credential_file'])).read_text().strip()
    if not KEY or not COLLEAGUE_KEY:
        raise RuntimeError('Empty provider credential')
    os.environ.update(COLLEAGUE_MODE='online', COLLEAGUE_MODEL=CFG['colleague_model'],
                      COLLEAGUE_MODEL_EFFORT=CFG['colleague_effort'],
                      COLLEAGUE_MODEL_BASE_URL=CFG['colleague_base_url'],
                      COLLEAGUE_MODEL_API_KEY=COLLEAGUE_KEY,
                      COWORK_COLLEAGUE_MAX_OUTPUT_TOKENS=str(CFG['colleague_max_output_tokens']))
    # Preserve the release's semantic/timeout fixes; the separately reviewed
    # current-release wrapper only sets outbound colleague token budgets.
    from current_sidecar_budget import install
    install(engine)


BLOCKED = set(CFG['blocked_rows'])
TERMINAL_STATES = {'finished', 'environment_error', 'agent_error', 'cancelled', 'grading_error', 'recovery_error'}
SYSTEM = """You are an autonomous software engineer working on a repository task.
Your objective is to implement the complete requested production functionality,
including integration, edge cases and regression safety. Read the task instruction,
inspect the repository, edit production code, and run meaningful tests. Superficial
assertions, hard-coded success values, prose-only answers and tests without the
required production implementation do not complete the task.
All task access is through the cowork MCP tools. The repository is /app/code in
the terminal tool's container. Read the public task instruction first: use the
task_instruction tool if registered; otherwise read /run/cowork-public/instruction.md
through the terminal tool.
Persist until the functional work and available validation are complete, or the
controller's eight-hour deadline expires. An API or tool failure is a reason to
diagnose and retry, not to assume completion. Save actual changes in the repository.
You may finish early when the requested functionality is implemented and verified.
Conclude with changes made, tests run and any concrete unresolved blockers.
"""


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def clean(value):
    text = str(value)
    for key in (KEY, COLLEAGUE_KEY):
        if key:
            text = text.replace(key, '[REDACTED]')
    return text


def entry(row):
    if row in BLOCKED:
        raise ValueError('Task is explicitly suspended: ' + str(row))
    matches = [e for e in CFG['tasks'] if e['row'] == row]
    if not matches and row in CFG.get('preflight_rows', {}).values():
        matches = [e for e in FROZEN_MANIFEST['tasks'] if e['row'] == row]
    if not matches:
        raise ValueError('Task is outside this shard and configured preflights')
    return matches[0]


def control():
    try:
        return json.loads((ROOT / 'CONTROL.json').read_text())
    except (OSError, ValueError):
        return {'concurrency': 0, 'pause_dispatch': True, 'stop': False}


def terminate(process):
    if process.poll() is not None:
        return
    try:
        # Let the driver interrupt the turn and persist its resume state before
        # terminating the app-server and MCP descendants as a last resort.
        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except ProcessLookupError:
        pass


def colleague_logs(state, job_root):
    identifier = state.get('containers', {}).get('workplace')
    if not identifier:
        return
    docker = engine.Docker()
    docker.owned(identifier, state['run_id'])
    # Locate only event ledgers; never export sidecar credentials or owner packs.
    result = docker.call(['exec', identifier, 'sh', '-c',
                          'find /run /tmp /workspace -maxdepth 5 -type f '
                          '\\( -name "colleague-model-events.jsonl" -o -name "workplace-events.jsonl" \\) 2>/dev/null'], check=False)
    out = job_root / 'sidecar-events'
    out.mkdir(exist_ok=True)
    saved = []
    for index, line in enumerate(result.stdout.splitlines()):
        if line.startswith(('/run/', '/tmp/', '/workspace/')) and Path(line).name in ('colleague-model-events.jsonl', 'workplace-events.jsonl'):
            target = out / f'{index:03d}-{Path(line).name}'
            cp = docker.call(['cp', identifier + ':' + line, target], check=False)
            if cp.returncode == 0:
                saved.append(str(target))
    log = docker.call(['logs', '--tail', '3000', identifier], check=False)
    (out / 'container.log').write_text(clean(log.stdout + '\n' + log.stderr))
    save(out / 'INDEX.json', {'event_files': saved, 'event_file_discovery_exit': result.returncode})


def colleague_backend_report(events, config):
    startups = [e for e in events if e.get('event') == 'runtime_started']
    settings = (startups[-1].get('colleague_reply') or {}) if startups else {}
    rendered = [e for e in events if e.get('event') == 'colleague_reply_rendered']
    failures = sum(e.get('event') == 'colleague_model_failed_closed' for e in events)
    phase_errors = [phase.get('error_code') for e in events for phase in e.get('phase_records', [])
                    if phase.get('error_code')]
    validated = []
    recovered = []
    for event in rendered:
        phases = event.get('phase_records') or []
        last = {p.get('phase'): p for p in phases}
        good = (event.get('model') == config['colleague_model']
                and event.get('effort') == config['colleague_effort']
                and event.get('fallback') is False and event.get('transaction_committed') is True
                and event.get('transport_status') == 'completed' and event.get('cache_hit') is False
                and bool(last) and all(name and p.get('status') == 'validated'
                    and p.get('transport_status') == 'completed' and not p.get('error_code')
                    for name, p in last.items()))
        if good:
            validated.append(event)
            recovered.extend(p['error_code'] for p in phases if p.get('error_code'))
    report = {'successful_replies': len(validated), 'rendered_replies': len(rendered),
              'invalid_rendered_replies': len(rendered)-len(validated), 'failed_replies': failures,
              'phase_errors': phase_errors, 'recovered_phase_errors': recovered,
              'model': settings.get('model'), 'effort': settings.get('effort'),
              'base_url': settings.get('base_url'), 'max_output_tokens': settings.get('max_output_tokens')}
    report['passed'] = bool(validated and len(validated) == len(rendered) and failures == 0
            and report['model'] == config['colleague_model']
            and report['effort'] == config['colleague_effort']
            and report['base_url'] == config['colleague_base_url']
            and report['max_output_tokens'] == config['colleague_max_output_tokens'])
    return report


def smoke(row, condition):
    with smoke_lock(ROOT, row, condition):
        return _smoke_once(row, condition)


def _smoke_once(row, condition):
    if condition not in CFG['conditions']:
        raise ValueError('Condition is disabled for this campaign')
    job_root, smoke_identity = reserve_smoke(ROOT, row, condition)
    job_id = smoke_identity['job_id']
    state = None
    report = {}
    try:
        with PREPARE_SLOTS:
            original_new_run = engine.new_run
            def remember_run(task, kind):
                nonlocal state
                created = original_new_run(task, kind)
                if created['row'] != row or created['kind'] != condition:
                    raise RuntimeError('Smoke runtime created the wrong task')
                state = created
                return created
            engine.new_run = remember_run
            try:
                state = engine.start(entry(row), oracle=condition == 'oracle')
            finally:
                engine.new_run = original_new_run
        configure_mcp(job_root, state)
        status = {'job_id': job_id, 'run_id': state['run_id'], 'row': row, 'condition': condition, 'agent_started_at': time.time()}
        prompt = ('This is a connectivity check, not the benchmark solve. Use the cowork terminal tool '
                  'to run: pwd; git rev-parse HEAD. Read the public instruction using task_instruction '
                  'if registered, or terminal head -c 500 /run/cowork-public/instruction.md otherwise. '
                  'Then reply TOOL_CONNECTIVITY_OK. Do not edit any file.')
        if condition == 'cowork':
            prompt += (' Before your final reply, use docs_search to find one public document and '
                       'docs_batch_get to read its title and version_history with include_history=true. '
                       'Use people_search to find a relevant colleague and chat_send to ask one concrete '
                       'question about this task\'s scope. Then you MUST call chat_fetch on that same '
                       'conversation to read the newly returned colleague reply, even when chat_send already '
                       'included its text; this separately verifies the fetch tool. '
                       'These are simulated benchmark colleagues; the question is part of this connectivity check.')
        result, metrics = attempt(job_root, state, status, 1, str(uuid.uuid4()), prompt, limit=360)
        init = metrics['init'] or {}
        registered = init.get('tools', [])
        allowed = (set(registered) == expected_tool_names(condition, row)
                   and all(s.get('name') == 'cowork' and not s.get('tools_error') for s in init.get('mcp_servers', [])))
        expected = len(expected_tool_names(condition, row))
        connected = any(s.get('name') == 'cowork' and s.get('status') == 'connected' for s in init.get('mcp_servers', []))
        required = {'mcp__cowork__terminal'}
        if row in (39, 93):
            required.add('mcp__cowork__task_instruction')
        if condition == 'cowork':
            required.update('mcp__cowork__' + t for t in ['docs_search','docs_batch_get','people_search','chat_send','chat_fetch'])
        required_called = required.issubset(metrics.get('successful_tools', []))
        report = {'condition': condition, 'row': row, 'run_id': state['run_id'], 'model': init.get('model'),
                  'registered_tools': registered, 'registered_tool_count': len(registered),
                  'only_task_tools': allowed, 'mcp_connected': connected,
                  'actual_tool_calls': metrics['tool_calls'], 'actual_tool_calls_by_name': metrics['tool_calls_by_name'],
                  'required_tools_called': required_called, 'cli_result': result,
                  'passed': (result['exit_code'] == 0 and not (result['result'] or {}).get('is_error', True)
                             and (result['result'] or {}).get('terminal_reason') == 'completed'
                             and required_called and not result['permission_denials'] and init.get('model') == CFG['model']
                             and allowed and connected and len(registered) == expected)}
    except Exception as exc:
        report = {'passed': False, 'condition': condition, 'error': clean(exc)}
    finally:
        if state:
            if condition == 'cowork':
                try:
                    colleague_logs(state, job_root)
                    events = []
                    for path in (job_root / 'sidecar-events').glob('*colleague-model-events.jsonl'):
                        for line in path.read_text().splitlines():
                            if line.strip():
                                events.append(json.loads(line))
                    backend = colleague_backend_report(events, CFG)
                    report['colleague_backend'] = backend
                    report['passed'] = bool(report.get('passed') and backend['passed'])
                except Exception as exc:
                    report['sidecar_log_warning'] = clean(exc)
                    report['passed'] = False
            stopped = engine.stop(engine.read_run(state['run_id']))
            report['cleanup_errors'] = stopped.get('cleanup_errors', [])
            if report['cleanup_errors']:
                report['passed'] = False
        report.update(effort=CFG['effort'], experiment_fingerprint=fingerprint(CFG),
                      provider_base_url=CFG['provider_base_url'])
        requests_path = job_root / 'OUTBOUND_REQUESTS.json'
        requests = json.loads(requests_path.read_text()) if requests_path.exists() else []
        request_check = outbound_report(requests, CFG)
        request_check['passed'] = outbound_passed(request_check)
        report['outbound_request_check'] = request_check
        report['passed'] = bool(report.get('passed') and request_check['passed'])
        report.setdefault('row', row)
        report.setdefault('run_id', state['run_id'] if state else None)
        report['freshness'] = freshness_report(job_root, state, smoke_identity)
        report['passed'] = bool(report.get('passed') and report['freshness']['passed'])
        publish_receipt(ROOT, job_root, smoke_identity, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def run_job(row, condition):
    job_id = f'{row:03d}-{condition}'
    job_root = ROOT / 'jobs' / job_id
    job_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = None
    status = {'schema': 'codex-eval-job-v1', 'job_id': job_id, 'row': row, 'condition': condition,
              'phase': 'preparing', 'started_at': time.time(), 'model': CFG['model'], 'effort': CFG['effort'],
              'session_id': str(uuid.uuid4()), 'retry_count': 0, 'tool_calls': 0, 'seed': CFG['seed'],
              'task_version': entry(row)['task_version'], 'task_package_sha256': entry(row)['source_package_sha256'],
              'budget_seconds': CFG['max_solver_seconds'], 'scaffold': CFG['scaffold'],
              'scaffold_version': CFG['scaffold_version'],
              'campaign_id': CFG['campaign_id'], 'colleague_model': CFG['colleague_model'],
              'colleague_effort': CFG['colleague_effort'], 'experiment_fingerprint': fingerprint(CFG),
              'runtime_policy_revision': CFG.get('runtime_policy_revision'),
              'runtime_variant': SELECTED_RUNTIME['variant'],
              'runtime_tree_sha256': SELECTED_RUNTIME['tree_sha256'],
              'runtime_registry_sha256': SELECTED_RUNTIME['registry_sha256']}
    previous_path = job_root / 'STATUS.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    if previous.get('phase') in TERMINAL_STATES:
        if previous.get('campaign_id') != CFG['campaign_id'] or previous.get('task_version') != status['task_version']:
            raise RuntimeError('Existing job belongs to a different experiment')
        return previous
    if previous and previous.get('phase') != 'recovery_pending':
        raise RuntimeError('Unfinished job requires explicit recovery; refusing overwrite')
    recovering = previous.get('phase') == 'recovery_pending'
    manage_resources = not recovering
    first_attempt = 1
    if recovering:
        if any(previous.get(key) != status.get(key) for key in (
                'row', 'condition', 'task_version', 'task_package_sha256',
                'campaign_id', 'experiment_fingerprint', 'runtime_variant',
                'runtime_tree_sha256', 'runtime_registry_sha256')):
            raise RuntimeError('Recovery identity differs from this task/runtime campaign')
        status = previous
        first_attempt = max([status.get('attempt', 0)] +
                            [int(p.name) for p in (job_root / 'attempts').glob('*') if p.name.isdigit()]) + 1
        status.update(phase='resuming', retry_count=first_attempt-1)
        status.setdefault('controller_recoveries', []).append({'at': time.time(), 'next_attempt': first_attempt})
    save(job_root / 'STATUS.json', status)
    try:
        if recovering:
            state = engine.read_run(status['run_id'])
            assert state['phase'] == 'solving' and state['row'] == row and state['kind'] == condition
            assert state['deadline'] == status['deadline'], 'Recovery must not reset the time budget'
            manage_resources = True
            engine.task_integrity(entry(row))
            engine.Docker().owned(state['containers']['main'], state['run_id'])
            goal = json.loads((job_root / 'GOAL_STATE.json').read_text())
            assert goal.get('threadId'), 'Recovery needs the original conversation'
            status['session_id'] = goal['threadId']
        else:
            with PREPARE_SLOTS:
                # Persist ownership immediately after native new_run(), before
                # image/container preparation can fail or the worker can exit.
                original_new_run = engine.new_run
                def bound_new_run(task, kind):
                    created = original_new_run(task, kind)
                    if created['row'] != row or created['kind'] != condition:
                        raise RuntimeError('Native runtime created the wrong task')
                    status['run_id'] = created['run_id']
                    save(job_root / 'STATUS.json', status)
                    return created
                engine.new_run = bound_new_run
                try:
                    state = engine.start(entry(row), oracle=condition == 'oracle')
                finally:
                    engine.new_run = original_new_run
            status.update(run_id=state['run_id'], agent_started_at=time.time(), deadline=state['deadline'])
        configure_mcp(job_root, state)
        manage_resources = True
        if not recovering:
            save(job_root / 'RUNTIME_STATE_INITIAL.json', state)
        condition_text = ('The instruction contains the complete specification. This is Oracle mode; implement it using code tools.'
                          if condition == 'oracle' else
                          'This is CoWork mode. Gather missing requirements through workplace tools and relevant colleague questions, then implement and verify the functionality.')
        prompt = ('Complete the repository development task. ' +
                  ('Read the public instruction with task_instruction now. ' if row in (39, 93) else
                   'Read /run/cowork-public/instruction.md through terminal now. ') + condition_text)
        if recovering:
            prompt = ('Continue the same repository task after the model connection stalled. '
                      'Keep the existing work and requirements; complete production implementation and meaningful validation. '
                      'The original eight-hour deadline is unchanged.')
        ending = 'agent_error'
        transport_wait = status.get('outer_retry_wait_seconds', 0.0)
        for number in range(first_attempt, CFG['max_transport_retries'] + 2):
            if STOP.is_set() or control().get('stop'):
                ending = 'cancelled'
                break
            if time.time() >= state['deadline']:
                ending = 'time_budget'
                break
            resume = number > 1
            result, metrics = attempt(job_root, state, status, number, status['session_id'], prompt, resume)
            final = result['result'] or {}
            if result['reason'] == 'operator_stop':
                ending = 'cancelled'
                break
            if result['reason'] == 'time_budget':
                ending = 'time_budget'
                break
            if result['exit_code'] == 0 and not final.get('is_error', True):
                ending = final.get('terminal_reason', 'completed')
                break
            error_text = clean(json.dumps(final) + (job_root / 'attempts' / f'{number:03d}' / 'stderr.log').read_text()[-6000:])
            # Transport/provider failures resume the same conversation and workspace.
            retryable = bool(re.search(r'429|50[0234]|52[024]|rate.?limit|overload|timeout|timed out|connection|ECONN|ETIMED|network|fetch failed|stream|capacity|temporar|API Error', error_text, re.I))
            from provider_result_policy import classify_native
            rejection = classify_native(final.get('error'))
            if rejection:
                status['failure_category'] = rejection
                retryable = False
            if final.get('terminal_reason') in ('registry_error', 'protocol_error') or re.search(r'\b(?:401|403)\b|invalid.?api.?key|authentication', error_text, re.I):
                retryable = False
            if not retryable or number > CFG['max_transport_retries']:
                status['agent_error'] = error_text[-2500:]
                break
            status.update(phase='retry_wait', retry_count=number, last_transport_error=error_text[-1000:])
            save(job_root / 'STATUS.json', status)
            delay = min(120, 5 * 2 ** min(number-1, 5)) + random.uniform(0, 3)
            wait_started = time.time()
            while time.time()-wait_started < delay and time.time() < state['deadline'] and not STOP.is_set():
                time.sleep(1)
            transport_wait += time.time()-wait_started
            status['outer_retry_wait_seconds'] = transport_wait
            save(job_root / 'STATUS.json', status)
            prompt = ('Continue the same repository task from the saved state after the provider interruption. '
                      'Inspect current work as needed, then complete production implementation and meaningful validation. '
                      'Do not discard completed changes or treat the interruption as task completion.')
        metrics = model_metrics(sorted((job_root / 'attempts').glob('*/trajectory.jsonl')))
        save(job_root / 'AGENT_METRICS.json', metrics)
        status.update(phase='waiting_for_grading', agent_end_reason=ending, agent_finished_at=time.time(),
                      agent_elapsed_seconds=time.time()-status['agent_started_at'],
                      outer_retry_wait_seconds=transport_wait, tool_calls=metrics['tool_calls'],
                      assistant_messages=metrics['assistant_messages'])
        save(job_root / 'STATUS.json', status)
        try:
            colleague_logs(state, job_root)
        except Exception as exc:
            status['sidecar_log_warning'] = clean(exc)
        # Codex and its MCP process group have exited. finish() stops all solver
        # descendants before any private verifier assets are mounted.
        with GRADE_SLOTS:
            status['phase'] = 'grading'
            save(job_root / 'STATUS.json', status)
            engine.task_integrity(entry(row))
            graded = engine.finish(state)
        save(job_root / 'GRADE.json', graded)
        status.update(phase='finished' if graded.get('execution_valid') else 'grading_error',
                      execution_valid=graded.get('execution_valid'), reward=graded.get('reward'),
                      agent_completed_normally=ending == 'completed')
    except Exception as exc:
        status.update(phase=('recovery_error' if not manage_resources else
                             'grading_error' if status.get('agent_finished_at') else 'environment_error'), error=clean(exc))
    finally:
        if state is None and manage_resources and status.get('run_id'):
            try:
                state = engine.read_run(status['run_id'])
            except Exception as exc:
                status['cleanup_errors'] = [clean(exc)]
        if state and manage_resources:
            try:
                if state.get('row') != row or state.get('kind') != condition:
                    raise RuntimeError('Cleanup state differs from worker task')
                current = engine.read_run(state['run_id'])
                if any(current.get(name) for name in ('containers', 'volumes', 'images', 'networks')):
                    current = engine.stop(current)
                status['cleanup_errors'] = current.get('cleanup_errors', [])
            except Exception as exc:
                status['cleanup_errors'] = [clean(exc)]
        status['finished_at'] = time.time()
        save(job_root / 'STATUS.json', status)
    return status


def cleanup_interrupted_job(row, condition):
    """Reclaim only the exact run recorded by this campaign worker."""
    job_root = ROOT / 'jobs' / f'{row:03d}-{condition}'
    path = job_root / 'STATUS.json'
    if not path.is_file():
        return {'job_id': job_root.name, 'phase': 'environment_error',
                'error': 'Worker exited before creating a task run', 'row': row, 'condition': condition}
    status = json.loads(path.read_text())
    if (status.get('row') != row or status.get('condition') != condition
            or status.get('campaign_id') != CFG['campaign_id']
            or status.get('experiment_fingerprint') != fingerprint(CFG)):
        raise RuntimeError('Refusing cleanup of another experiment')
    if any(status.get(key) != SELECTED_RUNTIME[source] for key, source in (
            ('runtime_variant', 'variant'), ('runtime_tree_sha256', 'tree_sha256'),
            ('runtime_registry_sha256', 'registry_sha256'))):
        raise RuntimeError('Refusing cleanup with a different task runtime')
    run_id = status.get('run_id')
    if run_id:
        state = engine.read_run(run_id)
        if state['row'] != row or state['kind'] != condition:
            raise RuntimeError('Refusing cleanup of another task run')
        state = engine.stop(state)
        status['cleanup_errors'] = state.get('cleanup_errors', [])
    if status.get('phase') not in TERMINAL_STATES:
        status.update(phase='recovery_error', error='Task worker exited without a terminal status')
    status['worker_cleanup_at'] = time.time()
    save(path, status)
    return status


def run_worker(row, condition):
    """Thread-pool jobs supervise processes; no task engine runs in a thread."""
    job_root = ROOT / 'jobs' / f'{row:03d}-{condition}'
    job_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    argv = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', condition, '--row', str(row)]
    signalled = False
    with (job_root/'worker.stdout.log').open('a') as out, (job_root/'worker.stderr.log').open('a') as err:
        process = subprocess.Popen(argv, cwd=ROOT, stdout=out, stderr=err,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        save(job_root/'WORKER_PROCESS.json', {'pid': process.pid, 'row': row,
             'condition': condition, 'campaign_id': CFG['campaign_id'], 'started_at': time.time()})
        while process.poll() is None:
            if (STOP.is_set() or control().get('stop')) and not signalled:
                process.send_signal(signal.SIGTERM)
                signalled = True
            time.sleep(0.5)
    path = job_root/'STATUS.json'
    status = json.loads(path.read_text()) if path.is_file() else {}
    if process.returncode or status.get('phase') not in TERMINAL_STATES:
        cleanup_argv = [sys.executable, '-B', str(Path(__file__).resolve()),
                        '--cleanup-worker', condition, '--row', str(row)]
        with (job_root/'worker.cleanup.log').open('a') as log:
            recovery = subprocess.run(cleanup_argv, cwd=ROOT, stdout=log,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        status = json.loads(path.read_text()) if path.is_file() else dict(
            job_id=job_root.name, row=row, condition=condition, campaign_id=CFG['campaign_id'],
            phase='recovery_error', error='Worker failed before creating a durable status')
        status.update(worker_returncode=process.returncode, cleanup_worker_returncode=recovery.returncode)
        if recovery.returncode:
            status['phase'] = 'recovery_error'
            status['cleanup_errors'] = status.get('cleanup_errors', []) + ['Independent worker cleanup failed; inspect worker.cleanup.log']
        save(path, status)
    return status


def summarize(active, pending, started_at, dispatch_observation=None):
    statuses = []
    for f in sorted((ROOT / 'jobs').glob('*/STATUS.json')):
        try:
            statuses.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
    counts = Counter(s['phase'] for s in statuses)
    configured = control()
    report = {'schema': 'codex-campaign-status-v1', 'pid': os.getpid(), 'started_at': started_at,
              'updated_at': time.time(), 'model': CFG['model'], 'effort': CFG['effort'],
              'eligible_tasks': len(CFG['tasks']), 'total_runs': len(CFG['tasks'])*len(CFG['conditions']),
              'blocked_task_rows': sorted(BLOCKED), 'requested_concurrency': configured.get('concurrency', 0),
              'active_jobs': len(active), 'pending_jobs': len(pending), 'phase_counts': dict(counts),
              'dispatch_control_observation': dispatch_observation,
              'dispatch_order': CFG['dispatch_order'],
              'pending_by_condition': dict(Counter(condition for row, condition in pending)),
              'active_by_condition': dict(Counter(s['condition'] for s in statuses if s['phase'] not in TERMINAL_STATES)),
              'active': [{k: s.get(k) for k in ['job_id','phase','attempt','retry_count','tool_calls','agent_elapsed_seconds','agent_pid']}
                         for s in statuses if s['phase'] not in TERMINAL_STATES],
              'completed': [{k: s.get(k) for k in ['job_id','phase','agent_end_reason','reward','execution_valid','agent_elapsed_seconds','tool_calls']}
                            for s in statuses if s['phase'] in TERMINAL_STATES]}
    save(ROOT / 'STATUS.json', report)
    return report


def oracle_dispatch_blocked(condition, active):
    return condition == 'oracle' and any(c == 'cowork' for _, c in active.values())


def resource_check():
    problems = []
    snapshot = {'checked_at': time.time(), 'free_gib': {}}
    for path, minimum_gib in CFG.get('disk_free_thresholds_gib', {'/': 15, '/data2': 100}).items():
        free_gib = shutil.disk_usage(path).free / (1024**3)
        snapshot['free_gib'][path] = free_gib
        if free_gib < minimum_gib:
            problems.append(f'{path}: only {free_gib:.1f} GiB free')
    mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available_gib = int(mem['MemAvailable'].strip().split()[0]) / (1024**2)
    snapshot['memory_available_gib'] = available_gib
    if available_gib < CFG.get('minimum_available_memory_gib', 12):
        problems.append(f'only {available_gib:.1f} GiB memory available')
    load = os.getloadavg()[0]
    snapshot['load1'] = load
    if load > max(16, (os.cpu_count() or 1) * 2):
        problems.append(f'host load too high: {load:.1f}')
    blocked = 0
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            blocked += stat.read_text().rsplit(')', 1)[1].split()[0] == 'D'
        except (OSError, IndexError):
            pass
    snapshot['d_state_processes'] = blocked
    if blocked >= 32:
        problems.append(f'{blocked} host processes stuck in D state')
    if not problems:
        started = time.monotonic()
        try:
            result = subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'],
                                    capture_output=True, text=True, timeout=8)
            if result.returncode or not result.stdout.strip():
                problems.append('dedicated Docker daemon is not responding successfully')
            else:
                snapshot['docker_version'] = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            problems.append('dedicated Docker daemon health check timed out or failed')
        snapshot['docker_probe_seconds'] = time.monotonic() - started
    snapshot['problems'] = problems
    save(ROOT / 'RESOURCE_HEALTH.json', snapshot)
    return problems


def infrastructure_failure(status):
    """Infrastructure errors must not drain the untouched queue into failures."""
    if status.get('phase') not in ('environment_error', 'grading_error'):
        return False
    detail = str(status.get('error', '')) + ' ' + str(status.get('cleanup_errors', []))
    return bool(re.search(r'docker|containerd|transport timed out', detail, re.I))


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--smoke', choices=['oracle','cowork'])
    commands.add_argument('--worker', choices=['oracle','cowork'], help=argparse.SUPPRESS)
    commands.add_argument('--cleanup-worker', choices=['oracle','cowork'], help=argparse.SUPPRESS)
    parser.add_argument('--row', type=int, default=None)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    verify_sources(ROOT, CFG, full_runtime=not args.cleanup_worker)
    if args.verify_only:
        if hashlib.sha256(Path(CFG['claude_binary']).read_bytes()).hexdigest() != CFG['claude_binary_sha256']:
            raise RuntimeError('Pinned Claude binary changed')
        print(json.dumps({'verified': True, 'harness': 'claude-code'}))
        return 0
    signal.signal(signal.SIGTERM, lambda *args: STOP.set())
    signal.signal(signal.SIGINT, lambda *args: STOP.set())
    condition = args.smoke or args.worker or args.cleanup_worker
    if condition:
        row = args.row if args.row is not None else CFG.get('preflight_rows', {}).get(condition)
        if row is None or (not args.smoke and args.row is None):
            raise ValueError('A task worker requires an explicit row')
        initialize_worker(row, condition, credentials=not args.cleanup_worker)
        if args.cleanup_worker:
            cleanup_interrupted_job(row, condition)
            return 0
        if hashlib.sha256(Path(CFG['claude_binary']).read_bytes()).hexdigest() != CFG['claude_binary_sha256']:
            raise RuntimeError('Pinned Claude binary changed')
        if args.smoke:
            return 0 if smoke(row, condition)['passed'] else 1
        directory = ROOT / 'jobs' / f'{row:03d}-{condition}'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory/'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_job(row, condition)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    probe_root = Path(CFG.get('provider_preflight_root', str(ROOT / 'preflight')))
    for condition in CFG['conditions']:
        probe_row = CFG.get('preflight_rows', {}).get(condition, 11)
        probe = load_receipt(probe_root, probe_row, condition)
        if not valid_receipt(probe, CFG, condition, probe_row):
            raise RuntimeError('Current experiment Codex/MCP/colleague preflight required: ' + condition)
    for path, expected in CFG['runtime_sha256'].items():
        for base in (ROOT / 'runtime-snapshot', Path(CFG['release_root'])):
            if hashlib.sha256((base / path).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Frozen runtime changed: ' + str(base / path))
    if hashlib.sha256((ROOT / 'MANIFEST.snapshot.json').read_bytes()).hexdigest() != CFG['manifest_sha256']:
        raise RuntimeError('Frozen manifest changed')
    for key, hash_key in [('claude_binary', 'claude_binary_sha256')]:
        if hashlib.sha256(Path(CFG[key]).read_bytes()).hexdigest() != CFG[hash_key]:
            raise RuntimeError('Pinned Codex binary changed')
    signal.signal(signal.SIGTERM, lambda *args: STOP.set())
    signal.signal(signal.SIGINT, lambda *args: STOP.set())
    (ROOT / 'jobs').mkdir(exist_ok=True)
    with (ROOT / 'controller.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started_at = time.time()
        save(ROOT / 'CONTROLLER.json', {'pid': os.getpid(), 'started_at': started_at,
             'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        pending, recoveries = [], []
        for condition in CFG['conditions']:
            for e in eligible_entries(CFG):
                f = ROOT / 'jobs' / f'{e["row"]:03d}-{condition}' / 'STATUS.json'
                if not f.exists():
                    pending.append((e['row'], condition))
                elif json.loads(f.read_text()).get('phase') == 'recovery_pending':
                    recoveries.append((e['row'], condition))
                elif json.loads(f.read_text()).get('phase') not in TERMINAL_STATES:
                    raise RuntimeError('Unfinished job from another controller requires explicit recovery: ' + str(f))
        pending = recoveries + pending
        recovery_jobs = set(recoveries)
        active = {}
        infrastructure_failures = []
        infrastructure_hold_until = 0
        dispatch_observation = None
        loop_sequence = 0
        with ThreadPoolExecutor(max_workers=CFG.get('maximum_concurrency', 10)) as pool:
            while active or pending:
                sampled_at = time.time()
                ctl = control()
                loop_sequence += 1
                if ctl.get('stop'):
                    STOP.set()
                target = max(0, min(CFG.get('maximum_concurrency', 10), int(ctl.get('concurrency', 10))))
                resource_blocks = resource_check()
                if time.time() < infrastructure_hold_until:
                    resource_blocks.append('repeated Docker failures; waiting before new dispatch')
                save(ROOT / 'RESOURCE_HOLD.json', {'at': time.time(), 'blocked': bool(resource_blocks),
                     'reasons': resource_blocks, 'infrastructure_hold_until': infrastructure_hold_until})
                if not STOP.is_set() and not resource_blocks:
                    while pending and len(active) < target:
                        if ctl.get('pause_dispatch') and pending[0] not in recovery_jobs:
                            break
                        if oracle_dispatch_blocked(pending[0][1], active):
                            break
                        row, condition = pending.pop(0)
                        recovery_jobs.discard((row, condition))
                        future = pool.submit(run_worker, row, condition)
                        active[future] = (row,condition)
                dispatch_observation = {'sampled_at': sampled_at, 'completed_at': time.time(),
                    'target_concurrency': target, 'control_concurrency': int(ctl.get('concurrency', 0)),
                    'pause_dispatch': bool(ctl.get('pause_dispatch')), 'stop': bool(ctl.get('stop')),
                    'loop_sequence': loop_sequence}
                summarize(active, pending, started_at, dispatch_observation)
                if STOP.is_set() and not active:
                    break
                if active:
                    done, _ = wait(active, timeout=10, return_when=FIRST_COMPLETED)
                    for future in done:
                        row, condition = active.pop(future)
                        try:
                            result = future.result()
                            if infrastructure_failure(result):
                                now = time.time()
                                infrastructure_failures = [t for t in infrastructure_failures if now-t < 300]
                                infrastructure_failures.append(now)
                                if len(infrastructure_failures) >= 3:
                                    infrastructure_hold_until = now + 120
                            print(json.dumps({k:result.get(k) for k in ['job_id','phase','reward','execution_valid','agent_elapsed_seconds','tool_calls','error']},ensure_ascii=False),flush=True)
                        except Exception as exc:
                            print(json.dumps({'row':row,'condition':condition,'controller_error':clean(exc)}),flush=True)
                else:
                    time.sleep(5)
        final = summarize(active, pending, started_at, dispatch_observation)
        final['controller_finished_at'] = time.time()
        save(ROOT / 'STATUS.json', final)
    return 0


from claude_adapter import install
install(sys.modules[__name__])

if __name__ == '__main__':
    sys.exit(main())

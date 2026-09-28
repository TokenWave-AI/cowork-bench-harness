#!/usr/bin/env python3
"""Read live colleague event counts; never expose facts or change a task."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / 'CONFIG.json').read_text())
ENV = {**os.environ, 'DOCKER_HOST': CFG['docker_host']}
PROGRAM = r'''
import json,time
from collections import Counter
from datetime import datetime
from pathlib import Path
counts, recent, errors, recent_errors = Counter(), Counter(), Counter(), Counter()
phase_errors, recent_phase_errors = Counter(), Counter()
phase_attempts = recent_phase_attempts = 0
last_success=None;last_failure=None
p=Path('/tmp/colleague-model-events.jsonl')
for line in p.read_text().splitlines() if p.exists() else []:
    try:e=json.loads(line)
    except ValueError:continue
    event=e.get('event')
    if event not in ('colleague_reply_rendered','colleague_model_failed_closed'):continue
    counts[event]+=1
    try:t=datetime.fromisoformat(e['at']).timestamp()
    except (KeyError,ValueError,TypeError):t=0
    if time.time()-t<600:recent[event]+=1
    phases=e.get('phase_records') or []
    phase_attempts += len(phases)
    if time.time()-t<600:recent_phase_attempts += len(phases)
    for phase in phases:
        code=phase.get('error_code')
        if code:
            phase_errors[code]+=1
            if time.time()-t<600:recent_phase_errors[code]+=1
    if event=='colleague_model_failed_closed':
        codes=e.get('error_codes') or [str(e.get('error_code'))]
        errors.update(codes)
        if time.time()-t<600:recent_errors.update(codes)
        last_failure=t
    else:last_success=t
print(json.dumps({'outcomes':counts,'recent_10m_outcomes':recent,'error_codes':errors,
                  'recent_10m_error_codes':recent_errors,'last_success_at':last_success,
                  'last_failure_at':last_failure, 'phase_attempts':phase_attempts,
                  'recent_10m_phase_attempts':recent_phase_attempts, 'phase_error_codes':phase_errors,
                  'recent_10m_phase_error_codes':recent_phase_errors}))
'''


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save(path, value):
    temporary = path.with_name(path.name + '.monitor-tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def inspect(job):
    status = read(job / 'STATUS.json')
    if status.get('condition') != 'cowork' or status.get('phase') not in ('solving','provider_wait','retry_wait'):
        return None
    state = read(ROOT / 'runs' / status.get('run_id', '') / 'session.json')
    identifier = state.get('containers', {}).get('workplace')
    if not identifier:
        return None
    try:
        result = subprocess.run(['docker','exec',identifier,'python3','-c',PROGRAM],
                                env=ENV, capture_output=True, text=True, timeout=20)
        if result.returncode:
            return {'job_id': job.name, 'read_error': result.stderr[:200]}
        return {'job_id': job.name, **json.loads(result.stdout)}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'job_id': job.name, 'read_error': type(error).__name__}


def main():
    with (ROOT / 'colleague-monitor.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            with ThreadPoolExecutor(max_workers=6) as pool:
                rows = [r for r in pool.map(inspect, sorted((ROOT/'jobs').glob('*'))) if r]
            valid = [r for r in rows if not r.get('read_error')]
            outcomes = sum((Counter(r['outcomes']) for r in valid), Counter())
            recent = sum((Counter(r['recent_10m_outcomes']) for r in valid), Counter())
            total = sum(recent.values())
            failures = recent['colleague_model_failed_closed']
            failure_rate = failures / total if total else None
            report = {'checked_at': time.time(), 'monitor_pid': os.getpid(),
                      'scope': 'currently active CoWork runs; ten-minute rolling window',
                      'active_cowork_rows_sampled': len(valid), 'read_errors': len(rows)-len(valid),
                      'outcomes': dict(outcomes), 'recent_10m_outcomes': dict(recent),
                      'recent_10m_failure_rate': failure_rate,
                      'error_codes': dict(sum((Counter(r['error_codes']) for r in valid), Counter())),
                      'phase_attempts': sum(r.get('phase_attempts',0) for r in valid),
                      'recent_10m_phase_attempts': sum(r.get('recent_10m_phase_attempts',0) for r in valid),
                      'phase_error_codes': dict(sum((Counter(r.get('phase_error_codes',{})) for r in valid), Counter())),
                      'recent_10m_phase_error_codes': dict(sum((Counter(r.get('recent_10m_phase_error_codes',{})) for r in valid), Counter())),
                      'rows_with_failures': sum(r['outcomes'].get('colleague_model_failed_closed',0)>0 for r in valid),
                      'rows_without_successful_reply': [r['job_id'] for r in valid if not r['outcomes'].get('colleague_reply_rendered')],
                      'health': 'degraded' if total>=10 and failure_rate>=0.2 else 'ok' if total else 'no_recent_samples',
                      'automatic_control_changes': False, 'rows': rows}
            save(ROOT/'COLLEAGUE_HEALTH.json', report)
            with (ROOT/'monitoring/colleague-health-history.jsonl').open('a') as log:
                log.write(json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False)+'\n')
            if read(ROOT/'STATUS.json').get('controller_finished_at'):
                return
            time.sleep(60)


if __name__ == '__main__':
    main()

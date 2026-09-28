#!/usr/bin/env python3
"""Gate new dispatch on observed provider failures, without making API calls."""
from collections import Counter
import fcntl
import json
import math
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
DONE = {'finished', 'environment_error', 'agent_error', 'cancelled', 'grading_error', 'recovery_error'}
OWNER = 'codex-provider-health-v1'


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save(path, value):
    temporary = path.with_name(path.name + '.health-tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def main():
    previous, progress_times = {}, {}
    recoveries, unhealthy = 0, 0
    with (ROOT / 'health.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            now = time.time()
            statuses = [read(p) for p in sorted((ROOT / 'jobs').glob('*/STATUS.json'))]
            jobs, retries = [], Counter()
            for s in statuses:
                if not s:
                    continue
                job = s['job_id']; calls = s.get('tool_calls', 0)
                if job in previous and calls > previous[job]:
                    progress_times[job] = now
                previous[job] = calls
                retries.update(s.get('native_api_retry_counts', {}))
                jobs.append({k: s.get(k) for k in ['job_id', 'condition', 'phase', 'tool_calls', 'retry_count',
                                                  'provider_waiting', 'native_api_retry_counts', 'provider_errors',
                                                  'transport_idle_seconds', 'transport_stall_restarts', 'pending_tool_calls']})
            # Saved jobs waiting for an admission slot have no model process.
            active = [j for j in jobs if j['phase'] in {'solving', 'provider_wait', 'retry_wait', 'resuming'}]
            waiting = [j for j in active if j['provider_waiting'] or j['phase'] == 'retry_wait']
            recent = [j for j in active if now - progress_times.get(j['job_id'], 0) < 120]
            with (ROOT / 'CONTROL.lock').open('a+') as control_lock:
                fcntl.flock(control_lock, fcntl.LOCK_EX)
                ctl = read(ROOT / 'CONTROL.json'); action = None
                if ctl.get('health_gate_owner') == OWNER and not ctl.get('stop'):
                    if len(waiting) >= max(2, math.ceil(len(active) * 0.5)):
                        unhealthy += 1; recoveries = 0
                        if unhealthy >= 2 and not ctl.get('pause_dispatch'):
                            ctl.update(pause_dispatch=True, pause_reason='Widespread provider retries in active jobs')
                            save(ROOT / 'CONTROL.json', ctl); action = 'paused_dispatch'
                    elif active and len(waiting) <= len(active) * 0.2 and len(recent) >= min(4, len(active)):
                        recoveries += 1; unhealthy = 0
                        if recoveries >= 2 and ctl.get('pause_dispatch'):
                            ctl.update(pause_dispatch=False, pause_reason=None)
                            save(ROOT / 'CONTROL.json', ctl); action = 'resumed_dispatch'
                    else:
                        unhealthy = 0; recoveries = 0
            report = {'checked_at': now, 'monitor_pid': os.getpid(), 'active_jobs': len(active),
                      'jobs_waiting_on_provider': len(waiting), 'jobs_with_tool_progress_last_120_seconds': len(recent),
                      'native_api_retry_counts': dict(retries), 'pause_dispatch': ctl.get('pause_dispatch'),
                      'jobs_without_output_3m': sum((j.get('transport_idle_seconds') or 0) >= 180 for j in active),
                      'transport_stall_restarts': sum(j.get('transport_stall_restarts') or 0 for j in jobs),
                      'action': action, 'jobs': jobs}
            save(ROOT / 'HEALTH.json', report)
            if action:
                with (ROOT / 'health-actions.jsonl').open('a') as f:
                    f.write(json.dumps({k: v for k, v in report.items() if k != 'jobs'}) + '\n')
            if read(ROOT / 'STATUS.json').get('controller_finished_at'):
                return
            time.sleep(15)


if __name__ == '__main__':
    main()

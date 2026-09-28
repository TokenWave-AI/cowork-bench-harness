"""Claude stream-json accounting and strict terminal-result validation."""
from collections import Counter
import json
from pathlib import Path

from preflight_binding import expected_tool_names, tool_result_success


def registry_valid(init, model, row, condition):
    if not isinstance(init, dict) or init.get('model') != model:
        return False
    tools = init.get('tools') or []
    servers = init.get('mcp_servers') or []
    return (len(tools) == len(set(tools)) and set(tools) == expected_tool_names(condition, row)
            and len(servers) == 1 and servers[0].get('name') == 'cowork'
            and servers[0].get('status') == 'connected')


def success(result, init, exit_code, model, session, row, condition):
    return bool(exit_code == 0 and isinstance(result, dict) and result.get('subtype') == 'success'
                and result.get('is_error') is False and result.get('session_id') == session
                and init and init.get('session_id') == session and registry_valid(init, model, row, condition)
                and not result.get('permission_denials'))


def model_metrics(paths):
    calls, messages, usage_by_message, successful = {}, set(), {}, set()
    errors, usage_reports, pending = [], [], set()
    init, result, last_event, last_at, progress_at = None, None, None, None, 0
    failures = set()
    for path in paths:
        if not Path(path).exists():
            continue
        pending.clear()
        with Path(path).open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                last_event = event.get('type', last_event)
                last_at = event.get('_controller_t', last_at)
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    init = event
                    pending.clear()
                if event.get('type') == 'result':
                    result = event
                    usage_reports.append({k: event.get(k) for k in ('uuid', 'session_id', 'usage', 'modelUsage', 'duration_ms', 'duration_api_ms', 'total_cost_usd')})
                    if event.get('is_error'):
                        errors.append({'at': last_at, 'errors': event.get('errors'), 'subtype': event.get('subtype')})
                if event.get('type') == 'system' and event.get('subtype') == 'api_retry':
                    errors.append({'at': last_at, **event})
                message = event.get('message') or {}
                if not isinstance(message, dict):
                    continue
                parts = message.get('content') or []
                if not isinstance(parts, list):
                    continue
                if event.get('type') == 'assistant':
                    mid = message.get('id') or event.get('uuid')
                    if mid:
                        messages.add(mid)
                        counts = usage_by_message.setdefault(mid, {})
                        for key, value in (message.get('usage') or {}).items():
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                counts[key] = max(counts.get(key, 0), value)
                    for part in parts:
                        if isinstance(part, dict) and part.get('type') == 'tool_use' and part.get('id'):
                            calls[part['id']] = part.get('name', 'unknown')
                            pending.add(part['id'])
                    progress_at = last_at or progress_at
                if event.get('type') == 'user':
                    for part in parts:
                        if not isinstance(part, dict) or part.get('type') != 'tool_result':
                            continue
                        tid = part.get('tool_use_id')
                        pending.discard(tid)
                        name = calls.get(tid)
                        content = part.get('content', [])
                        if isinstance(content, str):
                            content = [{'type': 'text', 'text': content}]
                        passed = name and not part.get('is_error') and tool_result_success({
                            'tool': name, 'status': 'completed', 'result': {'content': content, 'isError': bool(part.get('is_error'))}})
                        if passed:
                            successful.add(name)
                            progress_at = last_at or progress_at
                        else:
                            failures.add(tid)
    usage = Counter()
    for item in usage_by_message.values():
        usage.update(item)
    tools = Counter(calls.values())
    return {'assistant_messages': len(messages), 'tool_calls': len(calls), 'tool_calls_by_name': dict(tools),
            'successful_tools': sorted(successful), 'tool_error_events': len(failures),
            'observed_message_usage': dict(usage), 'cli_usage_reports': usage_reports,
            'usage_note': 'Message usage deduplicated by message id; per-invocation CLI totals preserved separately, never added to message totals.',
            'native_api_retry_counts': dict(Counter(str(e.get('subtype')) for e in errors if e.get('subtype') == 'api_retry')),
            'provider_errors': errors[-20:], 'last_event_type': last_event, 'last_event_at': last_at,
            'last_model_or_tool_progress_at': progress_at, 'pending_tool_calls': len(pending),
            'provider_waiting': bool(errors and (errors[-1].get('at') or 0) > progress_at),
            'init': init, 'result': result}

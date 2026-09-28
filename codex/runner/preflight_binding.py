"""Bind connectivity receipts to an experiment, not a previous provider run."""
import hashlib
import json
import re

COWORK_TOOLS = frozenset(['terminal', 'people_search', 'chat_check_updates', 'chat_fetch',
    'artifacts_search', 'docs_get', 'reviews_search', 'docs_search', 'workplace_help',
    'chat_search', 'chat_list', 'reviews_get', 'board_get', 'chat_send', 'board_batch_get',
    'artifacts_get', 'docs_batch_get', 'board_search'])


def expected_tool_names(condition, row):
    names = set(COWORK_TOOLS if condition == 'cowork' else ['terminal'])
    if row in (39, 93):
        names.add('task_instruction')
    return {'mcp__cowork__' + name for name in names}


def tool_result_success(item):
    if item.get('status') != 'completed' or item.get('error'):
        return False
    result = item.get('result')
    if not isinstance(result, dict) or result.get('isError') is True:
        return False
    payloads = [result]
    for block in result.get('content', []):
        if not isinstance(block, dict) or block.get('type') != 'text':
            continue
        text = block.get('text', '')
        if re.match(r'^\s*(?:RuntimeError\b|Traceback \(most recent call last\))', text):
            return False
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(decoded, dict):
            payloads.append(decoded)
    if any(p.get('isError') is True or p.get('error') or p.get('success') is False for p in payloads):
        return False
    terminal = str(item.get('tool') or item.get('name') or '').split('__')[-1] == 'terminal'
    if terminal:
        exits = [p['exit_code'] for p in payloads if 'exit_code' in p]
        return bool(exits) and all(type(code) is int and code == 0 for code in exits)
    return True


def registry_summary(registry, model):
    names, servers = [], []
    for server in registry.get('data', []):
        server_name = server.get('name', '')
        for name in server.get('tools', {}):
            names.append(name if name.startswith('mcp__') else f'mcp__{server_name}__{name}')
        servers.append({'name': server_name, 'status': server.get('runtimeStatus'),
                        'tools_error': server.get('toolsError')})
    return {'model': model, 'tools': names, 'mcp_servers': servers}


def eligible_entries(config):
    blocked = set(config.get('blocked_rows', []))
    return [entry for entry in config['tasks'] if entry['row'] not in blocked]


def fingerprint(config):
    payload = {k: v for k, v in config.items() if k not in ('experiment_fingerprint', 'created_at')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def outbound_report(records, config):
    responses = [r for r in records if str(r.get('path', '')).split('?', 1)[0].endswith('/responses')]
    return {'record_count': len(records), 'responses_count': len(responses),
            'requested_model': config['model'], 'requested_effort': config['effort'],
            'all_models_match': bool(responses) and all(r.get('model') == config['model'] for r in responses),
            'all_efforts_match': bool(responses) and all((r.get('reasoning') or {}).get('effort') == config['effort'] for r in responses),
            'any_successful_http': any(200 <= r.get('http_status', 0) < 300 for r in responses),
            'host_tools_exposed': any(r.get('rejected_host_tool_exposure') for r in records)}


def outbound_passed(report):
    return bool(report['all_models_match'] and report['all_efforts_match']
                and report['any_successful_http'] and not report['host_tools_exposed'])


def valid_receipt(receipt, config, condition, row):
    return bool(receipt.get('passed') is True and receipt.get('condition') == condition
                and receipt.get('row') == row and receipt.get('model') == config['model']
                and receipt.get('effort') == config['effort']
                and receipt.get('experiment_fingerprint') == fingerprint(config)
                and receipt.get('outbound_request_check', {}).get('passed') is True)

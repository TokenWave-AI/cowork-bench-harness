"""Identify a failed, safely retryable MCP startup; never accept its registry."""
from pathlib import Path

REVISION = 'bound-empty-mcp-startup-retry-v1'


def pending_init(event, *, session, model, workspace):
    return (isinstance(event, dict) and event.get('type') == 'system'
        and event.get('subtype') == 'init' and event.get('session_id') == session
        and event.get('model') == model and event.get('cwd') == str(Path(workspace))
        and event.get('tools') == []
        and event.get('mcp_servers') == [{'name': 'cowork', 'status': 'pending'}]
        and 'message' not in event)


def retryable_startup_result(final):
    # Controller-owned structured reason, not native/model/tool prose matching.
    return (isinstance(final, dict) and final.get('is_error') is True
        and final.get('terminal_reason') == 'mcp_startup_pending'
        and final.get('error') == {'code': 'mcp_startup_pending',
            'error': 'Bound cowork MCP initialization remained pending; retry the same session.'})


def startup_error():
    return {'code': 'mcp_startup_pending',
            'error': 'Bound cowork MCP initialization remained pending; retry the same session.'}

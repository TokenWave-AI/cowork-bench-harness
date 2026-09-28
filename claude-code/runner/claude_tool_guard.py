"""Recover only a pinned CLI's explicit rejection of an unavailable tool.

An assistant naming a tool does not register it. The init and outbound registry
checks remain authoritative. Every unregistered call must receive the exact
Claude Code 2.1.207 unavailable-tool error, tied to its call and session, before
the turn can succeed. Generic execution/permission errors are not such proof.
"""
import re


class ToolIsolationError(ValueError):
    pass


class UnregisteredToolGuard:
    rejection_timeout_seconds = 30

    def __init__(self, allowed, session):
        self.allowed = frozenset(allowed)
        self.session = session
        self.pending = {}
        self.rejected = []

    def tool_call(self, part, now):
        name, identifier = part.get('name'), part.get('id')
        if isinstance(name, str) and name in self.allowed:
            return
        if (not isinstance(identifier, str) or not identifier
                or not isinstance(name, str)
                or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,127}', name) is None):
            raise ToolIsolationError('unregistered_tool_identity_invalid')
        previous = self.pending.get(identifier)
        if previous is not None:
            if previous['name'] != name:
                raise ToolIsolationError('unregistered_tool_identity_changed')
            return
        if any(record['tool_use_id'] == identifier for record in self.rejected):
            raise ToolIsolationError('unregistered_tool_identity_reused')
        self.pending[identifier] = {'name': name, 'at': now}

    def tool_result(self, event, part, now):
        identifier = part.get('tool_use_id')
        if not isinstance(identifier, str):
            raise ToolIsolationError('tool_result_identity_invalid')
        call = self.pending.get(identifier)
        if call is None:
            return
        name = call['name']
        expected = ('<tool_use_error>Error: No such tool available: ' + name + '. '
                    + name + ' exists but is not enabled in this context. '
                    'Use one of the available tools instead.</tool_use_error>')
        message = event.get('message')
        message = message if isinstance(message, dict) else {}
        # Claude 2.1.207 omits the disabled-tool suffix for ToolSearch.
        # Accept only its exact native error envelope, never tool text.
        short_error = 'Error: No such tool available: ToolSearch'
        short_rejection = (name == 'ToolSearch'
            and part.get('type') == 'tool_result'
            and part.get('content') == '<tool_use_error>' + short_error + '</tool_use_error>'
            and message.get('content') == [part]
            and event.get('tool_use_result') == short_error)
        if (event.get('type') != 'user' or event.get('session_id') != self.session
                or message.get('role') != 'user'
                or part.get('is_error') is not True
                or not (part.get('content') == expected or short_rejection)):
            raise ToolIsolationError('unregistered_tool_not_explicitly_unavailable')
        if not 0 <= now - call['at'] <= self.rejection_timeout_seconds:
            raise ToolIsolationError('unregistered_tool_rejection_timeout')
        self.rejected.append({'tool_use_id': identifier, 'name': name,
                              'called_at': call['at'], 'rejected_at': now,
                              'proof': ('pinned-cli-toolsearch-no-such-tool-error' if short_rejection
                                        else 'pinned-cli-unavailable-tool-error'),
                              'registry_contained_tool': False})
        del self.pending[identifier]

    def check_timeout(self, now):
        if any(now - call['at'] > self.rejection_timeout_seconds for call in self.pending.values()):
            raise ToolIsolationError('unregistered_tool_rejection_timeout')

    def require_settled(self):
        if self.pending:
            raise ToolIsolationError('unregistered_tool_rejection_missing')

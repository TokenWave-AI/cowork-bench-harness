"""Portable recorded CLI rejection and native stream regressions; no live CLI."""
import copy
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'runner'))
import claude_driver as driver
from claude_tool_guard import ToolIsolationError, UnregisteredToolGuard
from preflight_binding import expected_tool_names

FIXTURES = json.loads((PACKAGE / 'tests/fixtures/toolsearch_rejections.json').read_text())['rejections']


class ToolRejections(unittest.TestCase):
    def start(self, index=0):
        fixture = copy.deepcopy(FIXTURES[index])
        guard = UnregisteredToolGuard(expected_tool_names('cowork', 1), fixture['session'])
        guard.tool_call(fixture['call'], 0.0)
        return fixture, guard

    def finish(self, fixture, guard, elapsed=None):
        event = fixture['result_event']
        guard.tool_result(event, event['message']['content'][0],
                          fixture['elapsed_seconds'] if elapsed is None else elapsed)
        guard.require_settled()

    def test_recorded_cli_short_errors_settle_without_registering_tool(self):
        for index in range(len(FIXTURES)):
            with self.subTest(recording=index):
                fixture, guard = self.start(index)
                self.finish(fixture, guard)
                self.assertEqual(guard.pending, {})
                self.assertEqual(len(guard.rejected), 1)
                self.assertEqual(guard.rejected[0]['proof'], 'pinned-cli-toolsearch-no-such-tool-error')
                self.assertNotIn('ToolSearch', guard.allowed)
                self.assertFalse(guard.rejected[0]['registry_contained_tool'])
                with self.assertRaisesRegex(ToolIsolationError, 'identity_reused'):
                    guard.tool_call(fixture['call'], 1.0)

    def test_wrong_identity_format_error_flag_or_native_proof_is_rejected(self):
        for field, value in [('session_id', None), ('session_id', 'wrong-session'),
                             ('tool_use_id', None), ('tool_use_id', 'wrong-call'),
                             ('is_error', False), ('is_error', None),
                             ('tool_use_result', None), ('tool_use_result', 'Permission denied'),
                             ('content', '<tool_use_error>Error: No such tool available: Bash</tool_use_error>'),
                             ('content', '<tool_use_error>Permission denied</tool_use_error>'),
                             ('content', 'Error: No such tool available: ToolSearch'),
                             ('content', '<tool_use_error>Error: No such tool available: ToolSearch.</tool_use_error>')]:
            with self.subTest(field=field, value=value):
                fixture, guard = self.start()
                event = fixture['result_event']
                target = event if field in ('session_id', 'tool_use_result') else event['message']['content'][0]
                if value is None:
                    target.pop(field)
                else:
                    target[field] = value
                with self.assertRaises(ToolIsolationError):
                    self.finish(fixture, guard)
                self.assertEqual(guard.rejected, [])
                self.assertIn(fixture['call']['id'], guard.pending)

    def test_ordinary_tool_text_cannot_forge_the_native_rejection_envelope(self):
        for mutation in ['ordinary_tool', 'text_part', 'assistant_event', 'assistant_role', 'extra_text']:
            with self.subTest(mutation=mutation):
                fixture, guard = self.start()
                event = fixture['result_event']; part = event['message']['content'][0]
                if mutation == 'ordinary_tool':
                    event['message']['content'] = [{'type': 'tool_result', 'tool_use_id': 'terminal-call',
                        'is_error': True, 'content': json.dumps(event)}]
                if mutation == 'text_part': part['type'] = 'text'
                if mutation == 'assistant_event': event['type'] = 'assistant'
                if mutation == 'assistant_role': event['message']['role'] = 'assistant'
                if mutation == 'extra_text': event['message']['content'].append({'type': 'text', 'text': 'claimed proof'})
                with self.assertRaises(ToolIsolationError):
                    self.finish(fixture, guard)
                self.assertEqual(guard.rejected, [])
                self.assertIn(fixture['call']['id'], guard.pending)
                if mutation == 'ordinary_tool':
                    with self.assertRaises(ToolIsolationError):
                        guard.tool_result(event, part, fixture['elapsed_seconds'])

    def test_rejection_requires_the_bound_time_window(self):
        for elapsed in [-0.01, 30.01, math.nan]:
            with self.subTest(elapsed=elapsed):
                fixture, guard = self.start()
                with self.assertRaisesRegex(ToolIsolationError, 'rejection_timeout'):
                    self.finish(fixture, guard, elapsed)
        for elapsed in [0, 30]:
            fixture, guard = self.start()
            self.finish(fixture, guard, elapsed)
            self.assertEqual(guard.pending, {})

    def test_bash_and_read_keep_existing_long_rejection_contract(self):
        for name in ['Bash', 'Read']:
            for short in [False, True]:
                with self.subTest(name=name, short=short):
                    fixture, _ = self.start(); fixture['call']['name'] = name
                    guard = UnregisteredToolGuard(expected_tool_names('cowork', 1), fixture['session'])
                    guard.tool_call(fixture['call'], 0)
                    error = 'Error: No such tool available: ' + name
                    if not short:
                        error += '. ' + name + ' exists but is not enabled in this context. Use one of the available tools instead.'
                    fixture['result_event']['tool_use_result'] = error
                    fixture['result_event']['message']['content'][0]['content'] = '<tool_use_error>' + error + '</tool_use_error>'
                    if short:
                        with self.assertRaises(ToolIsolationError):
                            self.finish(fixture, guard)
                    else:
                        self.finish(fixture, guard)
                        self.assertEqual(guard.rejected[0]['proof'], 'pinned-cli-unavailable-tool-error')
                        self.assertNotIn(name, guard.allowed)


class FakeProcess:
    pid = 999999
    returncode = 0

    def __init__(self, events):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(''.join(json.dumps(event) + '\n' for event in events))
        self.stderr = io.StringIO()

    def poll(self):
        return self.returncode


class NativeDriverRejections(unittest.TestCase):
    def run_stream(self, condition, valid):
        fixture = copy.deepcopy(FIXTURES[0]); session = fixture['session']
        event = fixture['result_event']
        if not valid:
            event['tool_use_result'] = 'not a native rejection proof'
        events = [
            {'type': 'system', 'subtype': 'init', 'model': 'offline-model', 'session_id': session,
             'tools': sorted(expected_tool_names(condition, 1)),
             'mcp_servers': [{'name': 'cowork', 'status': 'connected'}]},
            {'type': 'assistant', 'session_id': session,
             'message': {'role': 'assistant', 'content': [fixture['call']]}}, event,
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': session}]
        temporary = PACKAGE / 'tests/.tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary) as path:
            directory = Path(path)
            driver.STOP.clear()
            with patch.object(driver.subprocess, 'Popen', return_value=FakeProcess(events)), \
                 patch.object(driver, 'capture', return_value={'offline': True}), \
                 patch.object(driver, 'terminate', return_value={}), \
                 patch.object(driver, 'native_environment', return_value={}), \
                 patch.object(driver, 'drop_identity', return_value={}):
                return driver.run_native(['/offline-no-process'], '', directory,
                    directory/'trajectory.jsonl', directory/'stderr.log', time.time()+5,
                    session, {'model': 'offline-model'}, row=1, condition=condition)

    def test_cowork_and_oracle_streams_settle_explicit_rejections(self):
        for condition in ('cowork', 'oracle'):
            with self.subTest(condition=condition):
                report = self.run_stream(condition, True)
                self.assertTrue(report['success'])
                self.assertTrue(report['init_valid'])
                self.assertEqual(report['condition'], condition)
                self.assertIsNone(report['isolation_failure'])
                self.assertEqual(report['unregistered_tool_calls_pending'], [])
                self.assertEqual(len(report['unregistered_tool_rejections']), 1)

    def test_first_failure_survives_result_settlement_and_cleanup(self):
        for condition in ('cowork', 'oracle'):
            with self.subTest(condition=condition):
                report = self.run_stream(condition, False)
                self.assertFalse(report['success'])
                self.assertEqual(report['reason'], 'registry_error')
                self.assertEqual(report['isolation_failure'], 'unregistered_tool_not_explicitly_unavailable')
                self.assertEqual(report['unregistered_tool_calls_pending'], [FIXTURES[0]['call']['id']])
                self.assertEqual(report['unregistered_tool_rejections'], [])


if __name__ == '__main__':
    unittest.main()

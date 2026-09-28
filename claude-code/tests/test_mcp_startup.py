"""Portable MCP startup and same-session retry regressions; no live CLI."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'runner'))
import claude_driver as driver
import mcp_startup_policy as policy
from claude_goal import initialize

NATIVE_RUN = driver.run_native


def pending(workspace):
    return {'type': 'system', 'subtype': 'init', 'session_id': 'fixture-session',
            'model': 'offline-model', 'cwd': str(workspace), 'tools': [],
            'mcp_servers': [{'name': 'cowork', 'status': 'pending'}]}


class FakeProcess:
    pid = 999999
    returncode = 0

    def __init__(self, events):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(''.join(json.dumps(e) + '\n' for e in events))
        self.stderr = io.StringIO()

    def poll(self):
        return self.returncode


class MCPStartup(unittest.TestCase):
    def setUp(self):
        temporary = PACKAGE / 'tests/.tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.state = self.root / 'GOAL_STATE.json'
        self.deadline = time.time() + 1000
        initialize(self.state, 'fixture-session', self.workspace, self.deadline, 'objective', 'fingerprint')
        self.cfg = {'model': 'offline-model', 'effort': 'max', 'claude_binary': '/offline/claude',
                    'tasks': [{'row': 1}], 'conditions': ['cowork', 'oracle'],
                    'docker_host': 'unix:///offline.sock', 'native_solver_user': 'synthetic'}
        driver.STOP.clear()

    def run_stream(self, events, *, condition='cowork', target=None, deadline=None):
        target = target or self.root
        target.mkdir(parents=True, exist_ok=True)
        with patch.object(driver.subprocess, 'Popen', return_value=FakeProcess(events)), \
             patch.object(driver, 'capture', return_value={'session_id': 'fixture-session'}), \
             patch.object(driver, 'terminate', return_value={'clean': True}), \
             patch.object(driver, 'native_environment', return_value={}), \
             patch.object(driver, 'drop_identity', return_value={}):
            return NATIVE_RUN([], 'input', self.workspace, target/'trajectory.jsonl',
                target/'stderr.log', self.deadline if deadline is None else deadline,
                'fixture-session', self.cfg, self.state, row=1, condition=condition)

    def test_pending_init_fails_attempt_but_records_same_session_for_resume(self):
        for condition in ('cowork', 'oracle'):
            with self.subTest(condition=condition):
                result = self.run_stream([pending(self.workspace)], condition=condition,
                                         target=self.root/condition)
                self.assertEqual(result['reason'], 'mcp_startup_pending')
                self.assertFalse(result['success'])
                self.assertFalse(result['init_valid'])
                self.assertEqual(result['tools_seen'], [])
                goal = json.loads(self.state.read_text())
                self.assertTrue(goal['native_session_started'])
                self.assertEqual(goal['threadId'], 'fixture-session')
                self.assertEqual(goal['deadline'], self.deadline)

    def test_wrong_identity_extra_server_nonempty_tools_and_server_errors_fail_closed(self):
        changes = [{'session_id': 'other'}, {'model': 'other'}, {'cwd': '/other'},
            {'tools': ['Bash']}, {'tools': None}, {'mcp_servers': []},
            {'mcp_servers': [{'name': 'cowork', 'status': 'failed'}]},
            {'mcp_servers': [{'name': 'cowork', 'status': 'pending', 'error': 'failure'}]},
            {'mcp_servers': [{'name': 'cowork', 'status': 'pending'}, {'name': 'extra', 'status': 'pending'}]},
            {'mcp_servers': [{'name': 'other', 'status': 'pending'}]}]
        for condition in ('cowork', 'oracle'):
            for index, change in enumerate(changes):
                with self.subTest(condition=condition, change=change):
                    result = self.run_stream([dict(pending(self.workspace), **change)],
                        condition=condition, target=self.root/f'{condition}-{index}')
                    self.assertEqual(result['reason'], 'registry_error')
                    self.assertFalse(result['success'])

    def test_any_tool_activity_before_or_after_pending_init_remains_terminal(self):
        tool = {'type': 'assistant', 'session_id': 'fixture-session', 'message': {'role': 'assistant',
            'content': [{'type': 'tool_use', 'id': 'call-1', 'name': 'mcp__cowork__terminal', 'input': {}}]}}
        receipt = {'type': 'user', 'session_id': 'fixture-session', 'message': {'role': 'user',
                   'content': [{'type': 'tool_result', 'tool_use_id': 'unknown', 'content': 'text'}]}}
        for condition in ('cowork', 'oracle'):
            for index, events in enumerate(([tool, pending(self.workspace)], [pending(self.workspace), tool],
                                            [receipt, pending(self.workspace)], [pending(self.workspace), receipt])):
                with self.subTest(condition=condition, index=index):
                    result = self.run_stream(events, condition=condition,
                                             target=self.root/f'tool-{condition}-{index}')
                    self.assertEqual(result['reason'], 'registry_error')

    def test_native_result_or_second_init_cannot_be_hidden_as_pending(self):
        for index, followup in enumerate((pending(self.workspace),
            {'type': 'result', 'session_id': 'fixture-session', 'is_error': True, 'stop_reason': 'refusal'},
            {'type': 'result', 'session_id': 'fixture-session', 'is_error': False, 'subtype': 'success'})):
            result = self.run_stream([pending(self.workspace), followup], target=self.root/f'result-{index}')
            self.assertEqual(result['reason'], 'registry_error')

    def test_retry_requires_exact_controller_owned_reason_not_model_prose(self):
        final = {'is_error': True, 'terminal_reason': 'mcp_startup_pending', 'error': policy.startup_error()}
        self.assertTrue(policy.retryable_startup_result(final))
        for change in ({'is_error': False}, {'terminal_reason': 'registry_error'},
                       {'error': {'error': 'mcp_startup_pending'}},
                       {'error': {'code': 'provider_refusal', 'error': policy.startup_error()['error']}}):
            self.assertFalse(policy.retryable_startup_result(dict(final, **change)))

    def test_expired_budget_does_not_become_startup_retry(self):
        result = self.run_stream([pending(self.workspace)], deadline=time.time()-1)
        self.assertNotEqual(result['reason'], 'mcp_startup_pending')
        self.assertFalse(json.loads(self.state.read_text())['native_session_started'])

    def test_operator_stop_does_not_become_startup_retry(self):
        driver.STOP.set()
        self.addCleanup(driver.STOP.clear)
        result = self.run_stream([pending(self.workspace)])
        self.assertNotEqual(result['reason'], 'mcp_startup_pending')
        self.assertFalse(result['success'])

    def test_outer_attempts_preserve_original_session_budget_and_goal_history(self):
        root, workspace, cfg = self.root, self.workspace, self.cfg
        seen = []

        class Relay:
            errors = []
            child = None
            def __init__(self, *args): self.path = root/'absent-relay'
            def __enter__(self): return self
            def __exit__(self, *args): return None

        def native(argv, prompt, workdir, trajectory, stderr, deadline, session, cfg, state_path, *, row, condition):
            seen.append({'argv': argv, 'deadline': deadline, 'session': session,
                         'workspace': str(workdir), 'condition': condition})
            return self.run_stream([pending(workdir)], condition=condition,
                                    target=trajectory.parent, deadline=deadline)

        for condition in ('cowork', 'oracle'):
            run_id = f'fixture-{condition}'
            runs = root/condition/'runs'
            run = runs/run_id
            run.mkdir(parents=True)
            (run/'session.json').write_text(json.dumps({'root': str(run), 'run_id': run_id,
                                                       'row': 1, 'kind': condition}))
            state = root/condition/'GOAL_STATE.json'
            initialize(state, 'fixture-session', workspace, self.deadline, 'objective', 'fingerprint')
            original = json.loads(state.read_text())
            self.state = state
            common = dict(job=root/condition/'jobs/001-fixture', state=state, session='fixture-session',
                workspace=workspace, row=1, condition=condition, deadline=self.deadline,
                original_deadline=self.deadline, fingerprint='fingerprint', objective='objective',
                prompt='input', system='system', run_id=run_id)
            with patch.object(driver, 'MCPRelay', Relay), patch.object(driver, 'run_native', side_effect=native), \
                 patch.object(driver, 'native_environment', return_value={}), patch.object(driver, 'write_native_config'):
                for number in (1, 2):
                    target=root/condition/f'attempts/{number:03d}'
                    target.mkdir(parents=True)
                    result=driver.drive(cfg, target=target, resume=number > 1, **common)
                    self.assertEqual(result['terminal_reason'], 'mcp_startup_pending')
                    self.assertTrue(policy.retryable_startup_result(result))
            self.assertIn('--session-id', seen[-2]['argv'])
            self.assertIn('--resume', seen[-1]['argv'])
            self.assertEqual([s['deadline'] for s in seen[-2:]], [self.deadline, self.deadline])
            after=json.loads(state.read_text())
            self.assertEqual(after['turn_sequence'], 2)
            for key in ('created_at', 'threadId', 'workspace', 'deadline', 'experiment_fingerprint', 'history'):
                self.assertEqual(after[key], original[key])

    def test_complete_condition_specific_registry_is_still_required_for_success(self):
        for condition in ('cowork', 'oracle'):
            event = pending(self.workspace)
            event['tools'] = sorted(driver.expected_tool_names(condition, 1))
            event['mcp_servers'] = [{'name': 'cowork', 'status': 'connected'}]
            native_result = {'type': 'result', 'session_id': 'fixture-session',
                            'subtype': 'success', 'is_error': False, 'permission_denials': []}
            result = self.run_stream([event, native_result], condition=condition,
                                     target=self.root/f'complete-{condition}')
            self.assertTrue(result['success'])
            self.assertTrue(result['init_valid'])
            self.assertEqual(result['reason'], 'process_exit')


if __name__ == '__main__':
    unittest.main()

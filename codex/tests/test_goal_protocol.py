"""Execute the shipped Goal driver against a tiny real-stdio protocol peer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from fixtures import PACKAGE, fake_binary, save, temporary


class GoalProtocol(unittest.TestCase):
    def setUp(self):
        self.temp=temporary();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.binary=fake_binary(self.root)

    def run_driver(self, scenario='complete', resume=False, tools=18):
        state=self.root/'state.json';capture=self.root/(scenario+'-capture.jsonl')
        argv=[sys.executable,'-B',str(PACKAGE/'runner/goal_driver.py'),'--codex-bin',str(self.binary),
              '--model','offline-fixture-model','--effort','max','--cwd',str(self.root),
              '--instruction','Implement all requested behavior.','--objective','Implement and verify all requirements.',
              '--events-path',str(self.root/'events.jsonl'),'--state-path',str(state),
              '--deadline-seconds','0.3' if scenario=='deadline' else '10','--rpc-timeout','2',
              '--rate-limit-max-retries','20']
        if resume:argv.append('--resume')
        env=dict(os.environ,COWORK_EXPECTED_TOOLS=str(tools),FAKE_SCENARIO=scenario,FAKE_CAPTURE=str(capture),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=15)
        records=[json.loads(line) for line in capture.read_text().splitlines()]
        return result,json.loads(state.read_text()),records

    def test_initializes_tools_before_goal_and_collects_normal_terminal(self):
        result,state,requests=self.run_driver()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        methods=[r.get('method') for r in requests]
        self.assertEqual(methods[:5],['initialize','thread/start','mcpServerStatus/list','thread/goal/set','turn/start'])
        self.assertEqual(state['stopReason'],'goal-complete')
        self.assertEqual(state['tokensUsed'],120)
        self.assertEqual(state['turns'],1)
        self.assertTrue((self.root/'MCP_STATUS.json').is_file())
        self.assertIn('mcpToolCall',(self.root/'events.jsonl').read_text())

    def test_bad_tool_registry_fails_before_a_paid_turn_or_goal(self):
        result,state,requests=self.run_driver('bad_registry')
        self.assertEqual(result.returncode,2)
        self.assertEqual(state['stopReason'],'protocol-error')
        methods=[r.get('method') for r in requests]
        self.assertNotIn('turn/start',methods)
        self.assertNotIn('thread/goal/set',methods)

    def test_deadline_interrupts_authoritative_turn_and_saves_resume_handle(self):
        result,state,requests=self.run_driver('deadline')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(state['stopReason'],'deadline')
        interrupts=[r for r in requests if r.get('method')=='turn/interrupt']
        self.assertEqual(interrupts[0]['params'],{'threadId':'same-thread','turnId':'authoritative-1'})
        self.assertEqual(state['threadId'],'same-thread')

    def test_resume_uses_previous_thread_and_preserves_counter(self):
        save(self.root/'state.json',{'threadId':'same-thread','turns':4,'tokensUsed':70,
            'rateLimitRetries':[{'retry':1,'status':'resumed'}]})
        result,state,requests=self.run_driver(resume=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        methods=[r.get('method') for r in requests]
        self.assertIn('thread/resume',methods);self.assertNotIn('thread/start',methods)
        self.assertEqual(state['turns'],5)
        self.assertEqual(len(state['rateLimitRetries']),1)

    def test_native_automatic_continuation_does_not_schedule_duplicate_turn(self):
        result,state,requests=self.run_driver('automatic')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(state['turns'],2)
        self.assertEqual(sum(r.get('method')=='turn/start' for r in requests),1)

    def test_provider_refusal_is_not_normal_completion(self):
        result,state,requests=self.run_driver('refusal')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertEqual(state['stopReason'],'turn-failed')
        self.assertEqual(state['rateLimitRetries'],[])

    def test_provider_denial_interrupts_even_when_server_says_it_will_retry(self):
        result,state,requests=self.run_driver('refusal_notification')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertEqual(state['stopReason'],'provider_refusal')
        self.assertEqual(state['rateLimitRetries'],[])
        self.assertEqual(sum(r.get('method')=='turn/start' for r in requests),1)
        interrupts=[r for r in requests if r.get('method')=='turn/interrupt']
        self.assertEqual(interrupts[0]['params'],{'threadId':'same-thread','turnId':'authoritative-1'})

    def test_oracle_minimal_tool_inventory_is_accepted(self):
        result,state,requests=self.run_driver(tools=1)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        inventory=json.loads((self.root/'MCP_STATUS.json').read_text())
        self.assertEqual(list(inventory['data'][0]['tools']),['terminal'])


if __name__=='__main__':unittest.main()

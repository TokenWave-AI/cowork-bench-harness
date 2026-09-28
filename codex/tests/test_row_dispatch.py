"""Real dispatcher and shipped MCP proxy, with harmless per-row runtimes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from fixtures import PACKAGE, minimal_campaign, minimal_release, save, temporary


class RowDispatch(unittest.TestCase):
    def setUp(self):
        self.temp=temporary();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        # Tests may use the delivered release from any absolute location.
        release=os.environ.get('HARNESS_TEST_RELEASE')
        if not release:self.skipTest('Set HARNESS_TEST_RELEASE to the unpacked frozen release for dispatcher fixtures')
        self.release,self.tasks,self.registry=minimal_release(self.root,Path(release)/'runtime')
        self.campaign,self.config=minimal_campaign(self.root,self.release,self.tasks)
        self.env={k:v for k,v in os.environ.items() if not k.startswith('COWORK_')}
        self.env.update(COWORK_RUNS=str(self.campaign/'runs'),DOCKER_HOST='unix:///fixture.sock',PYTHONDONTWRITEBYTECODE='1')

    def invoke(self,argv,stdin=None):
        return subprocess.run([sys.executable,'-B',*map(str,argv)],input=stdin,env=self.env,
                              capture_output=True,text=True,timeout=10)

    def test_cli_start_selects_baseline_and_variant_in_distinct_processes(self):
        for number,variant in [(1,'baseline'),(2,'002')]:
            result=self.invoke([self.release/'runtime/row_dispatcher.py','start',str(number)])
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout)
            self.assertEqual(value['row'],number);self.assertEqual(value['variant'],variant)
            self.assertEqual(Path(value['engine']),self.release/('runtime' if number==1 else 'runtime/variants/002/runtime'))

    def test_proxy_keeps_bound_row_runtime_and_stdio(self):
        for number in (1,2):
            identifier=f'cw-{number:03d}-cowork-aaaaaaaaaaaa'
            runtime=self.release/('runtime' if number==1 else 'runtime/variants/002/runtime')
            state={'row':number,'run_id':identifier,'root':str(self.campaign/'runs'/identifier),'docker_host':self.env['DOCKER_HOST'],
                   **self.registry.state_binding(self.release,runtime,self.tasks[number-1])}
            save(self.campaign/'runs'/identifier/'session.json',state)
            result=self.invoke([self.campaign/'scripts/runtime_cli.py','mcp',identifier],
                json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n')
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout)['result']
            self.assertEqual(value['row'],number)
            self.assertEqual(value['variant'],'baseline' if number==1 else '002')

    def test_proxy_rejects_operator_lifecycle_as_an_agent_tool(self):
        result=self.invoke([self.campaign/'scripts/runtime_cli.py','finish','cw-001-cowork-aaaaaaaaaaaa'])
        self.assertNotEqual(result.returncode,0)
        self.assertIn('only permits mcp',result.stderr)

    def test_worker_cannot_reuse_engine_for_another_row(self):
        code=('import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import row_runtime;'
              'root=Path(sys.argv[2]);cfg=json.loads((root/"CONFIG.json").read_text());'
              'row_runtime.load_runtime(root,cfg,1,"cowork");row_runtime.load_runtime(root,cfg,2,"cowork")')
        result=self.invoke(['-c',code,self.campaign/'scripts',self.campaign])
        self.assertNotEqual(result.returncode,0)
        self.assertIn('exactly one task runtime',result.stderr)

    def test_hash_drift_is_rejected_before_import(self):
        (self.release/'runtime/row_dispatcher.py').write_text('raise RuntimeError("should not execute")\n')
        result=self.invoke([self.campaign/'scripts/runtime_cli.py','mcp','cw-001-cowork-aaaaaaaaaaaa'])
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Runtime bootstrap changed',result.stderr)
        self.assertNotIn('should not execute',result.stderr)


if __name__=='__main__':unittest.main()

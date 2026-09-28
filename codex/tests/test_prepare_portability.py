"""Portable prepare CLI must be offline, relocatable and credential-free."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from fixtures import PACKAGE, fake_binary, minimal_release, save, sha, temporary


class PreparePortability(unittest.TestCase):
    def setUp(self):
        self.temp=temporary();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        source=os.environ.get('HARNESS_TEST_RELEASE')
        if not source:self.skipTest('Set HARNESS_TEST_RELEASE for offline prepare fixtures')
        if not (PACKAGE/'harness.py').is_file():self.fail('harness.py is missing from deliverable')
        self.runtime_source=Path(source)/'runtime'

    def prepare(self,name):
        case=self.root/name;case.mkdir()
        # Running from two relocated source directories exposes accidental old
        # template imports and copied author-local runner paths.
        source=case/'relocated-harness';source.mkdir()
        shutil.copyfile(PACKAGE/'harness.py',source/'harness.py')
        shutil.copyfile(PACKAGE/'HARNESS.json',source/'HARNESS.json')
        shutil.copytree(PACKAGE/'runner',source/'runner',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        release,tasks,_=minimal_release(case,self.runtime_source)
        binary=fake_binary(case)
        host=case/'codex-code-mode-host';host.write_text('#!/bin/sh\nexit 0\n');host.chmod(0o755)
        credential=case/'external-provider.key'
        # It is deliberate that this configured file does not exist. Preparing
        # hashes/manifests must not read a live provider credential.
        self.assertFalse(credential.exists())
        cfg={'harness':'codex','campaign_id':'offline-fixture','release_root':str(release),
             'model':'fixture-model','effort':'max','provider':{'base_url':'https://provider.invalid/v1','api_key_file':str(credential)},
             'colleague':{'inherit':True,'max_output_tokens':32768},
             'binaries':{'codex':{'path':str(binary),'sha256':sha(binary)},
                         'code_mode_host':{'path':str(host),'sha256':sha(host)}},
             'conditions':['cowork','oracle'],'rows':[1,2],'concurrency':2,'budget_seconds':28800,
             'docker_host':'unix:///fixture-only.sock','native_solver_user':'nobody'}
        config_path=case/'input.json';save(config_path,cfg)
        output=case/'prepared'
        argv=[sys.executable,'-B',str(source/'harness.py'),'prepare','--config',str(config_path),'--output',str(output)]
        secret='offline-fixture-secret-never-copy-987654321'
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',OPENAI_API_KEY=secret,TOKENWAVE_API_KEY=secret)
        result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        rendered=json.loads((output/'CONFIG.json').read_text())
        self.assertEqual(rendered['model'],'fixture-model')
        self.assertEqual(rendered['max_solver_seconds'],28800)
        self.assertEqual(rendered['conditions'],['cowork','oracle'])
        self.assertEqual([t['row'] for t in rendered['tasks']],[1,2])
        self.assertEqual(Path(rendered['release_root']),release)
        self.assertFalse(credential.exists())
        self.assertFalse((output/'private/api-key').exists())
        for path in output.rglob('*'):
            if not path.is_file():continue
            text=path.read_bytes()
            self.assertNotIn(secret.encode(),text,str(path))
            self.assertNotIn(str(source).encode(),text,str(path))
            # Fixtures may live under PACKAGE/tests/.tmp; their deliberately
            # configured paths are not references to the original source.
            portable_text=text.replace(str(self.root).encode(),b'<offline-fixture>')
            self.assertNotIn(str(PACKAGE).encode(),portable_text,str(path))
        self.assertNotIn(secret,result.stdout+result.stderr)
        repeated=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
        self.assertNotEqual(repeated.returncode,0,'Existing prepared campaign must not be overwritten')
        verify=subprocess.run([sys.executable,'-B',str(output/'scripts/campaign.py'),'--verify-only'],
            env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(verify.returncode,0,verify.stdout+verify.stderr)
        return rendered,output,case

    def test_two_relocated_prepares_have_only_new_paths_and_no_credentials(self):
        first,first_output,first_case=self.prepare('first-location')
        second,second_output,second_case=self.prepare('second-location')
        for path in second_output.rglob('*'):
            if path.is_file():self.assertNotIn(str(first_case).encode(),path.read_bytes(),str(path))
        self.assertEqual(first['runtime_sha256'],second['runtime_sha256'])
        self.assertEqual(first['runner_source_sha256'],second['runner_source_sha256'])
        self.assertEqual(first['manifest_sha256'],second['manifest_sha256'])


if __name__=='__main__':unittest.main()

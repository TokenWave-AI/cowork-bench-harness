"""Small offline protocol/runtime fixtures; no Docker or model APIs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

PACKAGE = Path(os.environ.get('HARNESS_TEST_PACKAGE', str(Path(__file__).resolve().parents[1])))


def temporary():
    root = Path(os.environ.get('HARNESS_TEST_TMPDIR', str(Path(__file__).resolve().parent / '.tmp')))
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='offline-', dir=root)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


# The fixture implements JSON-RPC over real subprocess stdio. It does not open
# sockets, invoke Codex or read credentials. Hostile/error paths are structured.
FAKE_SERVER = r'''
import json,os,sys
scenario=os.environ.get('FAKE_SCENARIO','complete')
goal={'status':'active','objective':'fixture','tokensUsed':0,'tokenBudget':10000}
capture=os.environ['FAKE_CAPTURE']
def send(value):print(json.dumps(value),flush=True)
def event(method,params):send({'jsonrpc':'2.0','method':method,'params':params})
def turn(number,complete=True):
 ident='authoritative-'+str(number)
 event('turn/started',{'threadId':'same-thread','turn':{'id':ident,'status':'inProgress'}})
 if scenario=='deadline':return
 if scenario=='refusal_notification':
  event('error',{'threadId':'same-thread','turnId':ident,'willRetry':True,
    'error':{'message':'HTTP 403 Forbidden','codexErrorInfo':{'httpStatusCode':403}}})
  return
 event('item/completed',{'item':{'id':'tool-'+str(number),'type':'mcpToolCall','server':'cowork','tool':'terminal',
   'status':'completed','result':{'content':[{'type':'text','text':'{"exit_code":0,"stdout":"checked","stderr":""}'}],'isError':False}}})
 event('thread/tokenUsage/updated',{'tokenUsage':{'total':{'totalTokens':120,'inputTokens':100,'outputTokens':20}}})
 goal['status']='complete' if complete else 'active'
 if scenario=='refusal':goal['status']='blocked'
 event('thread/goal/updated',{'goal':dict(goal)})
 result={'id':ident,'status':'failed' if scenario=='refusal' else 'completed'}
 if scenario=='refusal':result['error']={'message':'HTTP 403 provider refused','codexErrorInfo':{'httpStatusCode':403}}
 event('turn/completed',{'turn':result})
for line in sys.stdin:
 request=json.loads(line)
 with open(capture,'a') as f:f.write(json.dumps(request)+'\n')
 if 'method' not in request or 'id' not in request:continue
 method=request['method'];params=request.get('params',{});out={}
 if method=='initialize':out={'codexHome':'fixture-only'}
 elif method in ('thread/start','thread/resume'):out={'thread':{'id':params.get('threadId','same-thread')}}
 elif method=='mcpServerStatus/list':
  count=int(os.environ['COWORK_EXPECTED_TOOLS']);tools={'terminal':{}}
  tools.update({'fixture_'+str(i):{} for i in range(count-1)})
  if scenario=='bad_registry':tools.pop('terminal')
  out={'data':[{'name':'cowork','tools':tools}]}
 elif method=='thread/goal/set':goal.update(params);out={'goal':dict(goal)}
 elif method=='thread/goal/get':out={'goal':dict(goal)}
 elif method=='turn/start':out={'turn':{'id':'provisional'}}
 elif method=='turn/interrupt':out={}
 else:
  send({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'unexpected '+method}});continue
 send({'jsonrpc':'2.0','id':request['id'],'result':out})
 if method=='turn/start':
  turn(1,scenario!='automatic')
  if scenario=='automatic':turn(2,True)
'''


def fake_binary(root):
    path = root / 'fake-codex'
    path.write_text('#!' + sys.executable + '\n' + FAKE_SERVER)
    path.chmod(0o755)
    return path


ENGINE = '''from pathlib import Path
import runtime_registry
RUNTIME_ROOT=Path(__file__).resolve().parent
ROOT=runtime_registry.release_root(RUNTIME_ROOT)
def task_integrity(entry):runtime_registry.assert_entry(ROOT,RUNTIME_ROOT,entry)
'''
CLI = '''import engine,json,os,sys
payload={'variant':os.environ['COWORK_RUNTIME_VARIANT'],'row':int(os.environ['COWORK_RUNTIME_ROW']),
         'engine':str(engine.RUNTIME_ROOT),'argv':sys.argv[1:]}
if sys.argv[1]=='mcp':
 for line in sys.stdin:
  request=json.loads(line)
  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':payload}),flush=True)
else:print(json.dumps(payload))
'''


def minimal_release(root, runtime_source):
    """Use the real portable dispatcher/registry with two non-Docker engines."""
    release = root / 'release'
    runtime = release / 'runtime'
    variant = runtime / 'variants/002/runtime'
    variant.mkdir(parents=True)
    for target in (runtime, variant):
        shutil.copyfile(runtime_source / 'runtime_registry.py', target / 'runtime_registry.py')
        (target / 'engine.py').write_text(ENGINE)
        (target / ('_row_cli.py' if target == runtime else 'cli.py')).write_text(CLI)
    shutil.copyfile(runtime_source / 'row_dispatcher.py', runtime / 'row_dispatcher.py')
    registry = module(runtime / 'runtime_registry.py', 'offline_fixture_registry')
    records = {}
    for name, folder in [('baseline', runtime), ('002', variant)]:
        files = registry.inventory(folder, baseline=name == 'baseline')
        records[name] = {'path':str(folder.relative_to(release)), 'files':files,
                         'tree_sha256':registry.tree_digest(files)}
    value = {'schema':registry.SCHEMA,'baseline':records['baseline'],'variants':{'002':records['002']},
             'rows':{'001':'baseline','002':'002'}}
    save(runtime / 'RUNTIME_VARIANTS.json', value)
    tasks = []
    for row, name in [(1, 'baseline'), (2, '002')]:
        path = release / f'tasks/{row:03d}-fixture'
        path.mkdir(parents=True)
        (path / 'instruction.md').write_text('Implement fixture behavior.\n')
        oracle = release / f'oracle/{row:03d}'
        oracle.mkdir(parents=True)
        (oracle / 'FULL_SPEC.md').write_text('Complete public fixture spec.\n')
        tasks.append({'row':row,'task_id':f'fixture-{row}','task_version':'fixture-v1','path':str(path.relative_to(release)),
            'budget_seconds':28800,'runtime_variant':name,'runtime_tree_sha256':records[name]['tree_sha256'],
            'source_package_sha256':'a'*64,'baseline':'b'*40,'files':{'instruction.md':sha(path/'instruction.md')},
            'oracle_path':f'oracle/{row:03d}/FULL_SPEC.md','oracle_sha256':sha(oracle/'FULL_SPEC.md'),
            'images':{'environment':'harbor.tokenwave.us/release/fixture@sha256:'+'c'*64,
                      'workplace':'harbor.tokenwave.us/release/fixture@sha256:'+'d'*64},
            'launch_status':'available','f2p_nodes':['feature'],'p2p_nodes':['regression']})
    save(release / 'MANIFEST.json', {'schema':'cowork-public-release-v1','release':'fixture-v1','task_count':2,'tasks':tasks,
        'runtime_integration':{'schema':registry.SCHEMA,'manifest':registry.REGISTRY,'sha256':sha(runtime/'RUNTIME_VARIANTS.json')}})
    (release/'cowork').write_text('#!/bin/sh\nexec python3 -B "$(dirname "$0")/runtime/row_dispatcher.py" "$@"\n')
    (release/'cowork').chmod(0o755)
    return release, tasks, registry


def minimal_campaign(root, release, tasks):
    output = root / 'campaign'
    (output/'scripts').mkdir(parents=True)
    for name in ('row_runtime.py','runtime_cli.py'):
        shutil.copyfile(PACKAGE/'runner'/name, output/'scripts'/name)
    shutil.copytree(release/'runtime',output/'runtime-snapshot/runtime')
    shutil.copyfile(release/'MANIFEST.json',output/'MANIFEST.snapshot.json')
    runtime_files = {str(path.relative_to(release)):sha(path) for path in (release/'runtime').rglob('*') if path.is_file()}
    config = {'release_root':str(release),'conditions':['cowork','oracle'],'tasks':tasks,'blocked_rows':[],
              'preflight_rows':{'cowork':1,'oracle':1},'max_solver_seconds':28800,'manifest_sha256':sha(release/'MANIFEST.json'),
              'runtime_sha256':runtime_files,'runtime_modes':{str(path.relative_to(release)):path.stat().st_mode&0o7777
                 for path in (release/'runtime').rglob('*') if path.is_file()},
              'runner_source_sha256':{str(path.relative_to(output)):sha(path) for path in (output/'scripts').iterdir()}}
    save(output/'CONFIG.json',config)
    return output,config

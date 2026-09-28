"""Apply a narrow output-budget override to a new, owned release-64 sidecar.

This runner overlay never changes the release, adapter, disclosure behavior,
model, effort, notification logic, or deadlines. Sources come from the selected
digest-pinned image or hash-bound runtime inputs. Mounted inputs are copied into
the current run before Docker create; shared and frozen sources stay unchanged.
Unknown source shapes fail explicitly. Receipts contain hashes, never source or
credentials. Row 087's native configuration is preserved without source changes.
"""
from __future__ import annotations

import ast
import copy
from functools import wraps
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time

BUDGET_ENV = 'COWORK_COLLEAGUE_MAX_OUTPUT_TOKENS'
DEFAULT_BUDGET = 32768
OVERLAY_REVISION = 'run-private-budget-mounts-v1'
ADAPTER = '/usr/local/bin/harbor_dynamic_colleague_adapter.py'
SEMANTIC = '/usr/local/bin/semantic_colleague.py'
NOTIFICATION = '/usr/local/bin/runtime_daemon.py'
NATIVE_HELPER = '/usr/local/bin/colleague_output_budget.py'
NATIVE_HELPER_SHA256 = '0565a90e8454b561b4d393adb687cc0fafc49865510b976bfeaf29b00ab30bf9'
PAYLOAD_KEYS = ['model', 'reasoning', 'instructions', 'input', 'max_output_tokens', 'stream']


class SidecarBudgetError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SidecarBudgetError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def configured_budget(environment=None):
    environment = os.environ if environment is None else environment
    value = environment.get(BUDGET_ENV, str(DEFAULT_BUDGET))
    require(isinstance(value, str) and value.isdecimal(), 'Invalid colleague budget configuration')
    budget = int(value)
    require(1 <= budget <= 131072, 'Colleague output budget outside [1, 131072]')
    return budget


def parsed(source, path):
    require(type(source) is bytes and 0 < len(source) <= 1_000_000,
            'Invalid sidecar source bytes: ' + path)
    try:
        tree = ast.parse(source, filename=path)
        compile(source, path, 'exec', dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        raise SidecarBudgetError('Invalid sidecar Python source: ' + path) from error
    return tree


def function(tree, name):
    candidates = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    require(len(candidates) == 1, 'Unknown function shape: ' + name)
    return candidates[0]


def keyword_default(node, name):
    pairs = list(zip(node.args.kwonlyargs, node.args.kw_defaults))
    values = [default for arg, default in pairs if arg.arg == name]
    require(len(values) == 1, 'Unknown keyword argument: ' + name)
    return values[0]


def native_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'max_output_tokens' and not node.args and not node.keywords)


def native_import(tree):
    return any(isinstance(n, ast.ImportFrom) and n.module == 'colleague_output_budget'
               and any(a.name == 'max_output_tokens' and a.asname is None for a in n.names)
               for n in tree.body)


def inspect_adapter(source, budget):
    tree = parsed(source, ADAPTER)
    factory = function(tree, 'build_runtime')
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'SemanticColleagueResponder']
    require(len(calls) == 1 and any(n is calls[0] for n in ast.walk(factory)),
            'Unknown semantic responder constructor')
    call = calls[0]
    require(not call.args and all(k.arg is not None for k in call.keywords),
            'Unrecognized positional or expanded semantic responder arguments')
    limits = [k.value for k in call.keywords if k.arg == 'max_output_tokens']
    require(len(limits) <= 1, 'Duplicate semantic budget')
    if not limits:
        mode = 'library-default'
    elif native_call(limits[0]) and native_import(tree):
        mode = 'native-environment-helper'
    elif isinstance(limits[0], ast.Constant) and type(limits[0].value) is int and limits[0].value == budget:
        mode = 'explicit-matching-budget'
    else:
        raise SidecarBudgetError('Unrecognized explicit adapter output budget')
    return {'sha256': sha(source), 'budget_mode': mode, 'source_mutated': False,
            'constructor_line': call.lineno}


def literal_patch(source, tree, node, budget, path, expected_original):
    require(type(budget) is int and 1 <= budget <= 131072, 'Invalid requested budget')
    require(isinstance(node, ast.Constant) and type(node.value) is int
            and node.value in {expected_original, budget}, 'Unknown budget literal: ' + path)
    original = node.value
    lines = source.splitlines(keepends=True)
    begin = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    require(source[begin:end] == str(original).encode(), 'Unsupported budget literal syntax')
    transformed = source[:begin] + str(budget).encode() + source[end:]
    expected_tree = copy.deepcopy(tree)
    matches = [n for n in ast.walk(expected_tree) if isinstance(n, ast.Constant)
               and getattr(n, 'lineno', None) == node.lineno and getattr(n, 'col_offset', None) == node.col_offset]
    require(len(matches) == 1, 'Ambiguous budget literal location')
    matches[0].value = budget
    changed = parsed(transformed, path)
    require(ast.dump(changed, include_attributes=False) == ast.dump(expected_tree, include_attributes=False),
            'Unexpected non-budget source changes')
    return transformed, {'original_sha256': sha(source), 'patched_sha256': sha(transformed),
                         'original_max_output_tokens': original, 'max_output_tokens': budget,
                         'line': node.lineno, 'only_budget_literal_changed': True,
                         'source_mutated': transformed != source, 'compile_passed': True}


def semantic_default(tree):
    assignments = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'DEFAULT_MAX_OUTPUT_TOKENS']
    require(len(assignments) == 1, 'Unknown semantic default declaration')
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SemanticColleagueResponder']
    require(len(classes) == 1, 'Unknown semantic responder class')
    constructor = function(classes[0], '__init__')
    request = function(tree, 'request_semantic_json')
    for fn in (constructor, request):
        default = keyword_default(fn, 'max_output_tokens')
        require(isinstance(default, ast.Name) and default.id == 'DEFAULT_MAX_OUTPUT_TOKENS',
                'Semantic budget no longer uses the audited default')
    attrs = [n for n in ast.walk(constructor) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                     and t.value.id == 'self' and t.attr == 'max_output_tokens' for t in n.targets)]
    require(len(attrs) == 1 and isinstance(attrs[0].value, ast.Name)
            and attrs[0].value.id == 'max_output_tokens', 'Unexpected responder budget assignment')
    return assignments[0].value


def payload_budget(fn):
    payloads = [n.value for n in fn.body if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'payload'
                and isinstance(n.value, ast.Dict)]
    require(len(payloads) == 1, 'Unknown request payload declaration')
    payload = payloads[0]
    require(all(isinstance(k, ast.Constant) for k in payload.keys)
            and [k.value for k in payload.keys] == PAYLOAD_KEYS, 'Unknown request payload fields')
    return payload.values[4]


def patch_semantic(source, budget, *, native=False):
    tree = parsed(source, SEMANTIC)
    node = semantic_default(tree)
    request_budget = payload_budget(function(tree, 'request_semantic_json'))
    require(isinstance(request_budget, ast.Name) and request_budget.id == 'max_output_tokens',
            'Semantic request does not forward its effective budget')
    if native:
        return source, {'original_sha256': sha(source), 'patched_sha256': sha(source),
                        'source_mutated': False, 'native_environment_helper': True,
                        'max_output_tokens': budget, 'compile_passed': True}
    return literal_patch(source, tree, node, budget, SEMANTIC, 4096)


def patch_notification(source, budget, *, native=False):
    tree = parsed(source, NOTIFICATION)
    value = payload_budget(function(tree, 'request_model_json'))
    if native:
        require(native_call(value) and native_import(tree), 'Native notification budget helper is missing')
        return source, {'original_sha256': sha(source), 'patched_sha256': sha(source),
                        'source_mutated': False, 'native_environment_helper': True,
                        'max_output_tokens': budget, 'compile_passed': True}
    return literal_patch(source, tree, value, budget, NOTIFICATION, 700)


def require_created(docker, state, identifier, image):
    require(state.get('phase') == 'preparing' and state.get('kind') == 'cowork'
            and state.get('containers', {}).get('workplace') == identifier,
            'Budget override requires a newly created CoWork sidecar')
    info = docker.owned(identifier, state['run_id'])
    labels = info.get('Config', {}).get('Labels') or {}
    require(info.get('Id') == identifier and labels.get('org.cowork.release.run') == state['run_id']
            and labels.get('org.cowork.release.role') == 'workplace', 'Sidecar ownership mismatch')
    lifecycle = info.get('State', {})
    require(lifecycle.get('Status') == 'created' and not lifecycle.get('Running')
            and str(lifecycle.get('StartedAt', '')).startswith('0001-')
            and info.get('RestartCount', 0) == 0, 'Sidecar was already started')
    require(info['Config'].get('Image') == image and '@sha256:' in image, 'Sidecar pinned image mismatch')
    return info


def read_source(docker, identifier, path, directory):
    destination = directory / Path(path).name
    docker.call(['cp', identifier + ':' + path, str(destination)])
    require(destination.is_file() and not destination.is_symlink(), 'Expected regular sidecar source: ' + path)
    require(destination.stat().st_size <= 1_000_000, 'Oversized sidecar source: ' + path)
    return destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode)


def canonical_regular(path):
    path = Path(path).absolute()
    require(path.resolve(strict=True) == path and not path.is_symlink() and path.is_file(),
            'Sidecar mount source must be a canonical regular file')
    require(path.stat().st_size <= 1_000_000, 'Oversized sidecar mount source')
    return path


def source_binding(engine, entry, source):
    """Only current frozen task/runtime inputs may become run-private copies."""
    source = canonical_regular(source)
    task = engine.task_path(entry).resolve(strict=True)
    runtime = Path(engine.RUNTIME_ROOT).resolve(strict=True)
    if source.is_relative_to(task):
        relative = source.relative_to(task).as_posix()
        expected = entry['files'].get(relative)
        kind = 'current-task-manifest'
    elif source.is_relative_to(runtime):
        relative = source.relative_to(runtime).as_posix()
        expected = (engine.RUNTIME_FILES.get(relative) or {}).get('sha256')
        kind = 'selected-runtime-manifest'
    else:
        raise SidecarBudgetError('Mounted budget source is outside the selected frozen task/runtime')
    raw = source.read_bytes()
    require(isinstance(expected, str) and len(expected) == 64 and sha(raw) == expected,
            'Mounted budget source differs from its current frozen binding')
    return raw, {'source': str(source), 'source_sha256': expected,
                 'source_binding': kind, 'source_relative': relative,
                 'source_mode': stat.S_IMODE(source.stat().st_mode)}


def private_run_root(engine, state, entry):
    root = Path(state['root']).absolute()
    require(state.get('phase') == 'preparing' and state.get('kind') == 'cowork'
            and state.get('row') == entry['row'] and not state.get('containers', {}).get('workplace'),
            'Mounted budget copies require a new CoWork run before container creation')
    require(root.resolve(strict=True) == root and root.name == state['run_id']
            and root.parent == engine.runs_root().resolve(strict=True)
            and not root.is_relative_to(Path(engine.ROOT).resolve(strict=True)),
            'Mounted budget copy destination is not this run directory')
    metadata = root.stat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and not stat.S_IMODE(metadata.st_mode) & 0o077,
            'Mounted budget copy run directory is not private to the runner')
    return root


def prepare_mounts(engine, state, entry, mounts, budget):
    """Before Docker create, replace exact library binds with new private copies.

    Shared inputs are read and hash-verified; they are never opened for writing.
    Copies are patched and sealed before Docker resolves their read-only mounts.
    """
    mounts = list(mounts)
    candidates = []
    targets = set()
    for index, item in enumerate(mounts):
        require(isinstance(item, (tuple, list)) and len(item) == 3, 'Unknown runtime mount shape')
        source, destination, readonly = item
        destination = str(destination)
        covered = [path for path in (SEMANTIC, NOTIFICATION)
                   if path == destination or path.startswith(destination.rstrip('/') + '/')]
        if not covered:
            continue
        require(destination in (SEMANTIC, NOTIFICATION) and covered == [destination],
                'A directory mount covers a budget library; explicit adaptation required')
        require(readonly is True and destination not in targets,
                'Budget library mount must be a unique read-only file binding')
        targets.add(destination)
        raw, binding = source_binding(engine, entry, source)
        if destination == SEMANTIC:
            transformed, change = patch_semantic(raw, budget)
        else:
            tree = parsed(raw, destination)
            native = native_call(payload_budget(function(tree, 'request_model_json'))) and native_import(tree)
            transformed, change = patch_notification(raw, budget, native=native)
        candidates.append((index, destination, raw, transformed, binding, change))
    if not candidates:
        return mounts, {}
    root = private_run_root(engine, state, entry)
    directory = Path(tempfile.mkdtemp(prefix='.colleague-budget-mounts-', dir=root))
    records = {}
    for index, destination, raw, transformed, binding, change in candidates:
        copied = directory / Path(destination).name
        descriptor = os.open(copied, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(transformed)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
        metadata = copied.stat()
        require(metadata.st_nlink == 1 and sha(copied.read_bytes()) == sha(transformed),
                'Run-private budget copy verification failed')
        original = canonical_regular(binding['source'])
        require(sha(original.read_bytes()) == binding['source_sha256'],
                'Frozen source changed while preparing the private copy')
        records[destination] = dict(binding, **change, run_id=state['run_id'], row=entry['row'],
                                    copy_root=str(directory), copy_path=str(copied),
                                    copy_sha256=sha(transformed), copy_device=metadata.st_dev,
                                    copy_inode=metadata.st_ino, copy_uid=metadata.st_uid,
                                    copy_mode=0o444, copy_links=1, target=destination,
                                    copied_before_container_create=True, readonly=True)
        mounts[index] = (copied, destination, True)
    engine.save(root / 'sidecar-budget-mounts.json', {
        'schema': OVERLAY_REVISION, 'status': 'prepared', 'run_id': state['run_id'],
        'row': entry['row'], 'max_output_tokens': budget, 'files': records,
        'frozen_sources_modified': False, 'mount_targets_unchanged': True})
    return mounts, records


def verify_prepared_mount(info, state, target, record):
    require(record.get('run_id') == state['run_id'] and record.get('row') == state['row']
            and record.get('target') == target and record.get('copied_before_container_create') is True,
            'Budget copy does not belong to this run and target')
    root = Path(state['root']).absolute()
    directory = Path(record['copy_root'])
    copied = Path(record['copy_path'])
    require(root.resolve(strict=True) == root and directory.parent == root
            and directory.name.startswith('.colleague-budget-mounts-')
            and directory.resolve(strict=True) == directory and copied.parent == directory
            and copied.name == Path(target).name,
            'Budget copy is outside its isolated run directory')
    directory_stat = directory.stat()
    require(directory_stat.st_uid == os.geteuid() and stat.S_IMODE(directory_stat.st_mode) == 0o700,
            'Budget copy directory ownership or mode changed')
    copied = canonical_regular(copied)
    metadata = copied.stat()
    require(metadata.st_dev == record['copy_device'] and metadata.st_ino == record['copy_inode']
            and metadata.st_uid == record['copy_uid'] == os.geteuid()
            and metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) == 0o444,
            'Budget copy inode, link count, ownership or permissions changed')
    raw = copied.read_bytes()
    require(sha(raw) == record['copy_sha256'], 'Budget copy changed before sidecar start')
    original = canonical_regular(record['source'])
    require(sha(original.read_bytes()) == record['source_sha256'], 'Original frozen budget source changed')
    actual = [mount for mount in info.get('Mounts', []) if mount.get('Destination') == target]
    require(len(actual) == 1 and actual[0].get('Type') == 'bind' and actual[0].get('RW') is False
            and actual[0].get('Source') == str(copied),
            'Container is not bound read-only to its exact run-private budget copy')
    return raw, stat.S_IMODE(metadata.st_mode)


def apply(docker, state, identifier, entry, save, budget, mounted_sources=None):
    mounted_sources = {} if mounted_sources is None else mounted_sources
    receipt = {'schema': 'current-sidecar-output-budget-v1', 'status': 'checking',
               'row': entry['row'], 'task_version': entry['task_version'], 'run_id': state['run_id'],
               'image': entry['images']['workplace'], 'max_output_tokens': budget,
               'before_first_start': True, 'files': {}, 'model_effort_timeouts_disclosure_unchanged': True,
               'overlay_revision': OVERLAY_REVISION}
    receipt_path = Path(state['root']) / 'sidecar-budget-patch.json'
    try:
        info = require_created(docker, state, identifier, entry['images']['workplace'])
        environment = dict(item.split('=', 1) for item in info['Config'].get('Env', []) if '=' in item)
        require(environment.get(BUDGET_ENV) == str(budget), 'Sidecar budget environment was not passed')
        receipt['effort'] = environment.get('COLLEAGUE_MODEL_EFFORT')
        # Shared mounts remain unwritable; only copies made by prepare_mounts
        # for this run may supply a mounted library. No Docker cp targets binds.
        for mounted in info.get('Mounts', []):
            destination = str(mounted.get('Destination', '')).rstrip('/')
            covered = [path for path in (SEMANTIC, NOTIFICATION)
                       if path == destination or path.startswith(destination + '/')]
            require(bool(destination), 'Unknown mount destination')
            if covered:
                require(covered == [destination] and destination in mounted_sources,
                        'Budget source is covered by an unprepared mount; explicit adaptation required')
                verify_prepared_mount(info, state, destination, mounted_sources[destination])
        for target, record in mounted_sources.items():
            require(target in (SEMANTIC, NOTIFICATION), 'Unknown prepared budget target')
            verify_prepared_mount(info, state, target, record)
        with tempfile.TemporaryDirectory(prefix='.current-sidecar-budget-', dir=state['root']) as temporary:
            directory = Path(temporary)
            adapter, _ = read_source(docker, identifier, ADAPTER, directory)
            # Docker cp implementations may not include stopped-container bind
            # sources. Inspect an exact read-only adapter mount directly instead.
            for mounted in info.get('Mounts', []):
                if mounted.get('Destination') != ADAPTER:
                    continue
                require(mounted.get('Type') == 'bind' and not mounted.get('RW'),
                        'Unknown adapter mount type or mutability')
                source = Path(mounted['Source'])
                require(source.is_file() and not source.is_symlink() and source.stat().st_size <= 1_000_000,
                        'Invalid selected runtime adapter mount')
                adapter = source.read_bytes()
                receipt['adapter_mount_sha256'] = sha(adapter)
            adapter_info = inspect_adapter(adapter, budget)
            receipt['files'][ADAPTER] = adapter_info
            native = adapter_info['budget_mode'] == 'native-environment-helper'
            if native:
                helper, _ = read_source(docker, identifier, NATIVE_HELPER, directory)
                require(sha(helper) == NATIVE_HELPER_SHA256, 'Unknown native budget helper source')
                receipt['files'][NATIVE_HELPER] = {'sha256': sha(helper), 'source_mutated': False}
            prepared = []
            for path, patcher in ((SEMANTIC, patch_semantic), (NOTIFICATION, patch_notification)):
                if path in mounted_sources:
                    source, mode = verify_prepared_mount(info, state, path, mounted_sources[path])
                else:
                    source, mode = read_source(docker, identifier, path, directory)
                transformed, record = patcher(source, budget, native=native)
                if path in mounted_sources:
                    require(source == transformed, 'Prepared mounted source has the wrong effective budget')
                    record = dict(mounted_sources[path], mutation_scope='run-private-readonly-bind',
                                  mounted_source_verified=True)
                receipt['files'][path] = record
                if source != transformed:
                    staged = directory / (Path(path).name + '.patched')
                    with staged.open('xb') as stream:
                        stream.write(transformed)
                    staged.chmod(mode)
                    prepared.append((path, staged, transformed))
            # Every shape validates before either library is modified.
            for path, staged, transformed in prepared:
                require_created(docker, state, identifier, entry['images']['workplace'])
                docker.call(['cp', str(staged), identifier + ':' + path])
                readback = directory / (Path(path).name + '.readback')
                docker.call(['cp', identifier + ':' + path, str(readback)])
                require(readback.is_file() and not readback.is_symlink() and readback.read_bytes() == transformed,
                        'Budget override readback mismatch: ' + path)
                receipt['files'][path]['readback_verified'] = True
            require_created(docker, state, identifier, entry['images']['workplace'])
            for target, record in mounted_sources.items():
                verify_prepared_mount(info, state, target, record)
        receipt.update(status='native-configuration-verified' if native else 'applied',
                       source_mutated=bool(prepared) or any(r['source_mutated'] for r in mounted_sources.values()),
                       frozen_sources_modified=False, completed_at=time.time())
        save(receipt_path, receipt)
        return receipt
    except Exception as error:
        receipt.update(status='failed', error_type=type(error).__name__, completed_at=time.time())
        if isinstance(error, SidecarBudgetError):
            receipt['reason'] = str(error)
        save(receipt_path, receipt)
        raise


def install(engine):
    original = engine.create
    if getattr(original, '_current_sidecar_budget', False):
        return
    require(not getattr(original, '_cowork_budget_wrapper', False), 'Old budget wrapper is already installed')

    @wraps(original)
    def create(state, role, *args, **kwargs):
        entry = None
        mounted_sources = {}
        if role == 'workplace' and not state.get('diagnostic_offline'):
            entries = [item for item in engine.manifest()['tasks'] if item['row'] == state['row']]
            require(len(entries) == 1, 'Missing or duplicate selected release task')
            entry = entries[0]
            budget = configured_budget()
            environment = dict(kwargs.get('environment') or {})
            environment[BUDGET_ENV] = str(budget)
            kwargs['environment'] = environment
            if kwargs.get('mounts'):
                kwargs['mounts'], mounted_sources = prepare_mounts(
                    engine, state, entry, kwargs['mounts'], budget)
        identifier = original(state, role, *args, **kwargs)
        if entry is not None:
            apply(engine.Docker(), state, identifier, entry, engine.save, budget, mounted_sources)
        return identifier

    create._current_sidecar_budget = True
    engine.create = create

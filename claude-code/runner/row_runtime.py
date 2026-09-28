"""Select an immutable per-row runtime before importing any runtime module."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys

_LOADED = None


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_registry(release):
    path = Path(release) / 'runtime/runtime_registry.py'
    spec = importlib.util.spec_from_file_location('_campaign_runtime_registry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_sources(root, config, *, full_runtime=False):
    """Hash checking uses config-owned paths; it never reads provider credentials."""
    root = Path(root)
    release = Path(config['release_root']).resolve(strict=True)
    for relative, expected in config['runner_source_sha256'].items():
        if sha(root / relative) != expected:
            raise RuntimeError('Candidate source changed: ' + relative)
    expected = config['manifest_sha256']
    if sha(root / 'MANIFEST.snapshot.json') != expected or sha(release / 'MANIFEST.json') != expected:
        raise RuntimeError('Release and frozen manifest snapshot differ')
    # Bootstrap code is executable trust input, so pin it before loading it.
    for relative in ('runtime/runtime_registry.py', 'runtime/row_dispatcher.py', 'runtime/RUNTIME_VARIANTS.json'):
        expected = config['runtime_sha256'].get(relative)
        if not expected or sha(release/relative) != expected:
            raise RuntimeError('Runtime bootstrap changed: ' + relative)
    frozen = json.loads((root / 'MANIFEST.snapshot.json').read_text())
    rows = {entry['row']: entry for entry in frozen['tasks']}
    if len(rows) != len(frozen['tasks']):
        raise RuntimeError('Duplicate row in frozen manifest')
    configured = config['tasks']
    if len({entry['row'] for entry in configured}) != len(configured):
        raise RuntimeError('Duplicate row in campaign shard')
    for entry in configured:
        if rows.get(entry['row']) != entry:
            raise RuntimeError('Campaign task differs from release manifest: ' + str(entry['row']))
        if entry['budget_seconds'] != config['max_solver_seconds']:
            raise RuntimeError('Campaign and task deadlines disagree')
    if full_runtime:
        expected_paths = set(config['runtime_sha256'])
        modes = config.get('runtime_modes', {})
        for base in (root / 'runtime-snapshot', release):
            actual = {p.relative_to(base).as_posix() for p in (base/'runtime').rglob('*')
                      if p.is_file() and not p.name.endswith(('.pyc', '.pyo'))
                      and not any(part in ('__pycache__', '.pytest_cache') for part in p.relative_to(base).parts)}
            if actual != expected_paths:
                raise RuntimeError('Runtime file inventory differs: ' + str(base))
            for relative, expected in config['runtime_sha256'].items():
                path = base / relative
                if path.is_symlink() or sha(path) != expected:
                    raise RuntimeError('Frozen runtime changed: ' + str(path))
                # Transport snapshots may be owner-only evidence copies.
                # Executable mode identity applies to the actual release tree.
                if base == release and relative in modes and path.stat().st_mode & 0o7777 != modes[relative]:
                    raise RuntimeError('Frozen runtime mode changed: ' + str(path))
    return rows


def selected_environment(release, selected, environment=None):
    env = dict(os.environ if environment is None else environment)
    env.update(COWORK_RELEASE_ROOT=str(release), COWORK_RUNTIME_ROOT=str(selected['runtime_root']),
               COWORK_RUNTIME_VARIANT=selected['variant'], COWORK_RUNTIME_ROW=str(selected['row']),
               PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join(dict.fromkeys([str(selected['runtime_root']), str(Path(release)/'runtime')])) )
    env.pop('COWORK_RUNTIME_VARIANTS', None)
    return env


def load_runtime(root, config, row, condition):
    global _LOADED
    root = Path(root).resolve(strict=True)
    release = Path(config['release_root']).resolve(strict=True)
    if condition not in config['conditions']:
        raise RuntimeError('Condition is disabled for this campaign')
    identity = (str(root), str(release), row, condition)
    if _LOADED:
        if _LOADED[0] != identity:
            raise RuntimeError('A worker may load exactly one task runtime')
        return _LOADED[1:]
    rows = verify_sources(root, config)
    eligible = {entry['row'] for entry in config['tasks']} | set(config.get('preflight_rows', {}).values())
    if row not in eligible or row in config.get('blocked_rows', []):
        raise RuntimeError('Row is outside this campaign shard')
    registry = load_registry(release)
    selected = registry.select(release, row)
    # An import in the parent/thread is a programming error, never silently reused.
    for name in ('engine', 'runtime_registry', 'oracle_runtime', 'gates'):
        if name in sys.modules:
            raise RuntimeError('Runtime was imported before row selection: ' + name)
    os.environ.update(selected_environment(release, selected))
    sys.path[:0] = list(dict.fromkeys([str(selected['runtime_root']), str(release/'runtime')]))
    engine = importlib.import_module('engine')
    if Path(engine.__file__).resolve() != selected['runtime_root']/'engine.py':
        raise RuntimeError('Wrong engine imported for task')
    registry.assert_entry(release, engine.RUNTIME_ROOT, rows[row])
    engine.task_integrity(rows[row])
    _LOADED = (identity, engine, selected)
    return engine, selected

#!/usr/bin/env python3
"""Prepare and run the public CoWork benchmark harness without private templates."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SCHEMA = 'cowork-public-harness-v1'
BINARY_KEYS = {'codex': ('codex', 'code_mode_host'), 'claude-code': ('claude',)}
FIELD = {'codex': 'codex_binary', 'code_mode_host': 'code_mode_host_binary', 'claude': 'claude_binary'}
HASH_FIELD = {'codex': 'codex_binary_sha256', 'code_mode_host': 'code_mode_host_sha256', 'claude': 'claude_binary_sha256'}
PRIVATE_NAMES = {'auth.json', 'config.toml', '.env'}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def url(value, label):
    parsed = urlsplit(value or '')
    require(parsed.scheme in ('https', 'http') and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            label + ' must be an HTTP(S) base URL without credentials or query parameters')
    return value.rstrip('/')


def external_credential(spec, root, release, role):
    allowed = {'base_url', 'api_key_file', 'api_key_env', 'display_name', 'inherit',
               'model', 'effort', 'max_output_tokens'}
    require(not set(spec) - allowed, 'Unknown credential/provider field')
    name = spec.get('api_key_env')
    require(name is None or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name), 'Invalid credential environment name')
    value = spec.get('api_key_file')
    require(value or name, role + ' needs api_key_file or api_key_env, never a raw API key')
    path = Path(value).expanduser().absolute() if value else root.parent / '.harness-credentials' / root.name / (role + '.key')
    require(path.resolve() == path and not path.is_relative_to(root) and not path.is_relative_to(release),
            'Credential file must be outside the campaign and release, with no symlinks')
    return str(path), name


def prepare(settings, output):
    require(isinstance(settings, dict), 'Configuration must be a JSON object')
    allowed = {'harness', 'release_root', 'model', 'effort', 'provider', 'colleague', 'binaries',
               'conditions', 'rows', 'concurrency', 'budget_seconds', 'docker_host', 'native_solver_user',
               'native_mcp_relay_root', 'preflight_rows', 'campaign_id', 'prepare_concurrency',
               'grading_concurrency', 'minimum_available_memory_gib', 'disk_free_gib', 'seed',
               'max_transport_retries', 'transport_idle_restart_seconds'}
    require(not set(settings) - allowed, 'Unknown setting: ' + ', '.join(sorted(set(settings) - allowed)))
    require(not re.search(r'(?<![A-Za-z0-9_])(?:sk-|ghp_|olp_)[A-Za-z0-9_.-]{16,}', json.dumps(settings)),
            'Inline credentials are forbidden; use a file path or environment variable name')
    kind = settings.get('harness')
    require(kind in BINARY_KEYS, 'harness must be codex or claude-code')
    marker = read(HERE / 'HARNESS.json')
    require(marker['harness'] == kind, 'This source package belongs to a different harness')
    root = Path(output).expanduser().absolute()
    release = Path(settings['release_root']).expanduser().resolve(strict=True)
    require(root.resolve() == root and not root.exists(), 'Output must be a new canonical directory')
    require(root != release and not root.is_relative_to(release) and not release.is_relative_to(root),
            'Campaign and release must be disjoint')
    require(not root.is_relative_to(HERE) and not HERE.is_relative_to(root), 'Campaign must be outside harness source')
    manifest = read(release / 'MANIFEST.json')
    entries = {entry['row']: entry for entry in manifest['tasks']}
    require(len(entries) == len(manifest['tasks']), 'Duplicate release task row')
    rows = settings.get('rows', 'all')
    if rows == 'all':
        rows = sorted(entries)
    require(isinstance(rows, list) and rows and all(type(r) is int for r in rows)
            and len(set(rows)) == len(rows) and set(rows) <= entries.keys(), 'Invalid task rows')
    conditions = settings.get('conditions', ['cowork'])
    require(conditions in (['cowork'], ['oracle'], ['cowork', 'oracle']),
            'conditions must be cowork, oracle, or cowork then oracle')
    budget = settings.get('budget_seconds', 28800)
    require(type(budget) is int and budget > 0 and all(entries[r]['budget_seconds'] == budget for r in rows),
            'Budget must match the frozen task manifest (this release uses 28800 seconds)')
    cap = settings.get('concurrency', 10)
    require(type(cap) is int and 1 <= cap <= 80, 'concurrency must be between 1 and 80')
    require(settings.get('max_transport_retries', 20) == 20, 'This reviewed runner uses 20 transport retries')
    if set(rows) & {39, 93}:
        require(root.is_relative_to('/data2'), 'Broker tasks 039/093 require campaign storage under /data2')
    model = settings.get('model')
    effort = settings.get('effort', 'max')
    require(isinstance(model, str) and model.strip() and '\n' not in model, 'model is required')
    require(effort in ('low', 'medium', 'high', 'xhigh', 'max'), 'Unsupported effort value')
    provider = settings['provider']
    solver_file, solver_env = external_credential(provider, root, release, 'solver')
    colleague = settings.get('colleague', {'inherit': True})
    if colleague.get('api_key_file') or colleague.get('api_key_env'):
        colleague_file, colleague_env = external_credential(colleague, root, release, 'colleague')
    else:
        colleague_file, colleague_env = solver_file, solver_env
    base_url = url(provider['base_url'], 'provider.base_url')
    colleague_url = colleague.get('base_url', base_url if kind == 'codex' else None)
    require(colleague_url or 'cowork' not in conditions, 'Claude CoWork requires a colleague Responses base_url')
    colleague_url = url(colleague_url or base_url, 'colleague.base_url')
    probes = settings.get('preflight_rows') or {condition: rows[0] for condition in conditions}
    require(set(probes) == set(conditions) and all(r in rows for r in probes.values()),
            'preflight_rows must select an included row for each condition')
    docker = settings.get('docker_host', 'unix:///var/run/docker.sock')
    require(isinstance(docker, str) and docker.startswith('unix:///'), 'Use a local Unix Docker socket')
    binaries = {}
    for name in BINARY_KEYS[kind]:
        spec = settings['binaries'][name]
        path = Path(spec['path']).expanduser().resolve(strict=True)
        require(path.is_file() and os.access(path, os.X_OK), name + ' must be executable')
        digest = sha(path)
        if spec.get('sha256'):
            require(digest == spec['sha256'], name + ' binary SHA256 mismatch')
        binaries[FIELD[name]] = str(path)
        binaries[HASH_FIELD[name]] = digest
    if kind == 'codex':
        host = Path(binaries['code_mode_host_binary'])
        require(host.name == 'codex-code-mode-host' and host.parent == Path(binaries['codex_binary']).parent,
                'Place the matching codex-code-mode-host next to codex')
    tree = {}
    modes = {}
    for path in sorted((release / 'runtime').rglob('*')):
        if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
            continue
        require(not path.is_symlink(), 'Runtime source symlinks are not supported')
        if path.is_file():
            relative = path.relative_to(release).as_posix()
            tree[relative] = sha(path)
            modes[relative] = path.stat().st_mode & 0o7777
    require(all(x in tree for x in ('runtime/runtime_registry.py', 'runtime/row_dispatcher.py',
                                    'runtime/RUNTIME_VARIANTS.json')), 'Release needs per-row runtime dispatcher')
    identity = settings.get('campaign_id', root.name)
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', identity), 'Invalid campaign_id')
    cfg = dict(schema=SCHEMA, harness=kind, campaign_id=identity, model=model, effort=effort,
        provider_base_url=base_url, provider_display_name=provider.get('display_name', 'configured-provider'),
        credential_file=solver_file, colleague_credential_file=colleague_file,
        credential_environment_sources={'solver': solver_env, 'colleague': colleague_env},
        colleague_model=colleague.get('model', model), colleague_effort=colleague.get('effort', effort),
        colleague_base_url=colleague_url, colleague_max_output_tokens=colleague.get('max_output_tokens', 32768),
        docker_host=docker, release_root=str(release), tasks=[entries[r] for r in sorted(rows)],
        blocked_rows=[], conditions=conditions, seed=settings.get('seed'), scaffold=marker['scaffold'],
        scaffold_version=marker['cli_version'], max_solver_seconds=budget, max_transport_retries=20,
        maximum_concurrency=cap, preflight_rows=probes, dispatch_order='cowork-before-oracle',
        prepare_concurrency=settings.get('prepare_concurrency', min(2, cap)),
        grading_concurrency=settings.get('grading_concurrency', min(2, cap)),
        runtime_sha256=tree, runtime_modes=modes, manifest_sha256=sha(release / 'MANIFEST.json'),
        runtime_policy_revision=marker['runtime_policy_revision'],
        minimum_available_memory_gib=settings.get('minimum_available_memory_gib', 12),
        disk_free_thresholds_gib={str(root): settings.get('disk_free_gib', 50)},
        transport_idle_restart_seconds=settings.get('transport_idle_restart_seconds', 600), **binaries)
    for name in ('prepare_concurrency', 'grading_concurrency'):
        require(type(cfg[name]) is int and 1 <= cfg[name] <= cap, name + ' must be within the job cap')
    if kind == 'claude-code':
        require(isinstance(settings.get('native_solver_user'), str) and settings['native_solver_user'],
                'Claude requires a provisioned non-root native_solver_user')
        cfg.update(native_solver_user=settings['native_solver_user'], native_mcp_relay_root=str(root / 'mcp-relays'))
        require(len(str(root / 'mcp-relays')) + 38 < 108, 'Campaign path is too long for Unix MCP sockets')
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name in ('scripts', 'runtime-snapshot', 'jobs', 'runs', 'sessions', 'preflight', 'tmp',
                 'cache', 'logs', 'monitoring', 'agent-workdirs', 'mcp-relays'):
        (root / name).mkdir(mode=0o700)
    for path in sorted((HERE / 'runner').glob('*.py')):
        target = root / 'scripts' / path.name
        shutil.copyfile(path, target)
        target.chmod(0o600)
    for relative in tree:
        target = root / 'runtime-snapshot' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(release / relative, target)
    shutil.copyfile(release / 'MANIFEST.json', root / 'MANIFEST.snapshot.json')
    cfg['runner_source_sha256'] = {path.relative_to(root).as_posix(): sha(path)
                                  for path in sorted((root / 'scripts').glob('*.py'))}
    if kind == 'codex':
        cfg['goal_driver_sha256'] = sha(root / 'scripts/goal_driver.py')
    write(root / 'CONFIG.json', cfg)
    write(root / 'CONTROL.json', {'concurrency': cap, 'pause_dispatch': False, 'stop': False})
    immutable = ['CONFIG.json', 'MANIFEST.snapshot.json', *cfg['runner_source_sha256']]
    write(root / 'READY.json', {'schema': SCHEMA, 'created_at': time.time(),
          'files': {name: sha(root / name) for name in immutable}})
    if kind == 'claude-code':
        root.chmod(0o711)
        (root / 'scripts').chmod(0o711)
        (root / 'scripts/claude_mcp_socket.py').chmod(0o644)
        (root / 'agent-workdirs').chmod(0o711)
        (root / 'mcp-relays').chmod(0o711)
    return {'prepared': str(root), 'harness': kind, 'tasks': len(rows), 'conditions': conditions,
            'credentials_read': False, 'api_calls': 0, 'docker_calls': 0}


def verify(root, *, user=False):
    root = Path(root).resolve(strict=True)
    ready = read(root / 'READY.json')
    require(ready.get('schema') == SCHEMA, 'Incomplete or unsupported prepared root')
    for name, digest in ready['files'].items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'Invalid immutable path')
        require(sha(root / name) == digest, 'Prepared configuration/source changed: ' + name)
    cfg = read(root / 'CONFIG.json')
    for name in BINARY_KEYS[cfg['harness']]:
        require(sha(cfg[FIELD[name]]) == cfg[HASH_FIELD[name]], 'Pinned executable changed: ' + name)
    environment = dict(os.environ, TMPDIR=str(root / 'tmp'), XDG_CACHE_HOME=str(root / 'cache'),
                       PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable, '-B', str(root / 'scripts/campaign.py'), '--verify-only'],
                   check=True, cwd=root, env=environment, timeout=180, stdout=subprocess.DEVNULL)
    if user and cfg['harness'] == 'claude-code':
        account = pwd.getpwnam(cfg['native_solver_user'])
        require(account.pw_uid != 0 and account.pw_gid != 0, 'Native Claude identity must be non-root')
        require(os.geteuid() == 0, 'Claude controller requires root to launch its separate native solver user')
        for parent in root.parents:
            require(parent.stat().st_mode & 0o001, 'Native solver needs traversal permission on ' + str(parent))
    return cfg


def credentials(root, cfg):
    locations = {}
    for role, key in [('solver', 'credential_file'), ('colleague', 'colleague_credential_file')]:
        if role == 'colleague' and 'cowork' not in cfg['conditions']:
            continue
        path = Path(cfg[key])
        env_name = cfg['credential_environment_sources'].get(role)
        require(path.resolve() == path and not path.is_symlink() and not path.is_relative_to(root),
                'Credential path must be external and regular')
        if path in locations:
            require(locations[path] == env_name, 'Conflicting shared credential sources')
            continue
        locations[path] = env_name
        if not path.exists():
            value = os.environ.get(env_name or '', '').strip()
            require(value and '\n' not in value and '\r' not in value, 'Missing provider credential environment variable')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(value + '\n')
        require(path.is_file() and path.stat().st_mode & 0o077 == 0,
                'Credential file must have owner-only permissions (0600)')


def status(root):
    cfg = read(root / 'CONFIG.json')
    jobs = [read(path) for path in sorted((root / 'jobs').glob('*/STATUS.json'))]
    total = len(cfg['tasks']) * len(cfg['conditions'])
    return {'harness': cfg['harness'], 'model': cfg['model'], 'planned_runs': total,
            'not_started': total - len(jobs), 'phases': dict(Counter(job.get('phase') for job in jobs)),
            'normal_completions': sum(job.get('agent_completed_normally') is True for job in jobs),
            'jobs': [{k: job.get(k) for k in ('job_id', 'phase', 'agent_end_reason', 'reward',
                     'agent_elapsed_seconds', 'tool_calls', 'retry_count', 'error')} for job in jobs]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare_parser = sub.add_parser('prepare')
    prepare_parser.add_argument('--config', type=Path, required=True)
    prepare_parser.add_argument('--output', type=Path, required=True)
    for command in ('doctor', 'smoke', 'run', 'status'):
        item = sub.add_parser(command)
        item.add_argument('--root', type=Path, required=True)
        if command == 'smoke':
            item.add_argument('--condition', choices=['cowork', 'oracle'], default='cowork')
            item.add_argument('--row', type=int)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        print(json.dumps(prepare(read(args.config), args.output), ensure_ascii=False))
        return 0
    root = args.root.resolve(strict=True)
    if args.command == 'status':
        print(json.dumps(status(root), indent=2, ensure_ascii=False))
        return 0
    cfg = verify(root, user=args.command != 'doctor')
    if args.command == 'doctor':
        print(json.dumps({'passed': True, 'harness': cfg['harness'], 'tasks': len(cfg['tasks']),
                          'api_calls': 0, 'docker_calls': 0,
                          'note': 'Source/binary integrity only; run live smoke before model evaluation.'}))
        return 0
    credentials(root, cfg)
    command = [sys.executable, '-B', str(root / 'scripts/campaign.py')]
    if args.command == 'smoke':
        require(args.condition in cfg['conditions'], 'Condition is disabled')
        row = cfg['preflight_rows'][args.condition]
        require(args.row is None or args.row == row, 'Smoke row must match prepared preflight_rows')
        command += ['--smoke', args.condition, '--row', str(row)]
    env = dict(os.environ, TMPDIR=str(root / 'tmp'), XDG_CACHE_HOME=str(root / 'cache'),
               DOCKER_HOST=cfg['docker_host'], PYTHONDONTWRITEBYTECODE='1')
    return subprocess.call(command, cwd=root, env=env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('harness: ' + str(error), file=sys.stderr)
        raise SystemExit(2)

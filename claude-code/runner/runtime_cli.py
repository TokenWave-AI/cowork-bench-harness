#!/usr/bin/env python3
"""Delegate task MCP/lifecycle commands to the release's verified row dispatcher."""
import json
import os
from pathlib import Path
import sys
from row_runtime import load_registry, verify_sources

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    config = json.loads((ROOT/'CONFIG.json').read_text())
    verify_sources(ROOT, config)
    release = Path(config['release_root']).resolve(strict=True)
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] != 'mcp':
        raise RuntimeError('Solver runtime proxy only permits mcp <run-id>')
    registry = load_registry(release)
    dispatcher_path = release/'runtime/row_dispatcher.py'
    import importlib.util
    spec = importlib.util.spec_from_file_location('_campaign_dispatcher', dispatcher_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault('runtime_registry', registry)
    spec.loader.exec_module(module)
    selected = module.run_row(release, arguments[1])
    rows = {entry['row'] for entry in config['tasks']} | set(config.get('preflight_rows', {}).values())
    if selected['row'] not in rows or selected['row'] in config.get('blocked_rows', []):
        raise RuntimeError('MCP run is outside this campaign shard')
    declared = os.environ.get('COWORK_RUNTIME_ROW')
    if declared is not None and int(declared) != selected['row']:
        raise RuntimeError('MCP run differs from worker row')
    # exec preserves the MCP process group and exact stdio transport, so native
    # CLI/driver group cleanup also reaps the per-row task MCP descendants.
    env = module.child_environment(release, selected)
    os.execve(sys.executable, [sys.executable, '-B', str(dispatcher_path), *arguments], env)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('cowork: ' + str(error), file=sys.stderr)
        sys.exit(1)

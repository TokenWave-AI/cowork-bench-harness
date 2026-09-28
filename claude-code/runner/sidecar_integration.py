"""Keep colleague output budgets uniform across generic and broker launches."""
from functools import wraps
from sidecar_budget import (BUDGET_ENV, NATIVE_CONFIGURED_BUDGETS,
                            configured_budget, patch_created_sidecar)


def install(engine, semantic_timeout_repair_rows=()):
    repair_rows = set(semantic_timeout_repair_rows)
    if repair_rows and repair_rows != {2}:
        raise ValueError('Only the independently reviewed row002 timeout repair is supported')
    original = engine.create
    if getattr(original, '_cowork_budget_wrapper', False):
        return

    @wraps(original)
    def create(state, role, *args, **kwargs):
        entry = None
        prepared_adapter = None
        if role == 'workplace' and not state.get('diagnostic_offline'):
            entry = next(e for e in engine.manifest()['tasks'] if e['row'] == state['row'])
            if entry['row'] == 2:
                from public_sidecar_mount import prepare_adapter
                kwargs['mounts'], prepared_adapter = prepare_adapter(
                    engine, state, entry, kwargs.get('mounts') or [], configured_budget())
            native = NATIVE_CONFIGURED_BUDGETS.get(entry['row'])
            if native and entry.get('task_version') == native['task_version']:
                environment = dict(kwargs.get('environment') or {})
                environment[BUDGET_ENV] = str(configured_budget())
                kwargs['environment'] = environment
        identifier = original(state, role, *args, **kwargs)
        if entry is not None:
            patch_created_sidecar(engine.Docker(), state, identifier, entry, engine.save,
                                  prepared_adapter=prepared_adapter)
            if state['row'] in repair_rows:
                from semantic_timeout_patch import patch_created_sidecar as repair_timeout
                repair_timeout(engine.Docker(), state, identifier, entry, engine.save)
        return identifier

    create._cowork_budget_wrapper = True
    engine.create = create

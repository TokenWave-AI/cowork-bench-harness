"""Configure output budgets in a newly created owned sidecar.

Audited native revisions receive the budget through configuration and their code
is verified without modification. Legacy revisions receive a narrowly checked
source patch before first start. No owner pack, credential or model prompt is exported.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import tempfile
import time

from sidecar_budget_sources import EXPECTED_ADAPTER_SHA256, EXPECTED_NOTIFICATION_SHA256

ADAPTER = "/usr/local/bin/harbor_dynamic_colleague_adapter.py"
NOTIFICATION = "/usr/local/bin/runtime_daemon.py"
BUDGET_ENV = "COWORK_COLLEAGUE_MAX_OUTPUT_TOKENS"
DEFAULT_BUDGET = 32768
NATIVE_CONFIGURED_BUDGETS = {
    87: {
        'task_version': 'held-repair-087-native-budget-lineage-20260926-v2',
        'files': {
            '/usr/local/bin/harbor_dynamic_colleague_adapter.py': '3353a7fa13c5a9598651ced8dd5de5ae6b90296db27e4f05ccd2570654151f28',
            '/usr/local/bin/runtime_daemon.py': '4e402d3120769d1edc08e822336e8f6f055e097389c33d6acd920b686ed6fd7a',
            '/usr/local/bin/colleague_output_budget.py': '0565a90e8454b561b4d393adb687cc0fafc49865510b976bfeaf29b00ab30bf9',
        },
    },
}
RUN_LABEL = "org.cowork.release.run"
ROLE_LABEL = "org.cowork.release.role"


class SidecarBudgetError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SidecarBudgetError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_budget(value):
    require(type(value) is int and 1 <= value <= 131072,
            "Colleague output budget must be an integer between 1 and 131072")
    return value


def configured_budget(environment=None):
    environment = os.environ if environment is None else environment
    raw = environment.get(BUDGET_ENV, str(DEFAULT_BUDGET))
    require(isinstance(raw, str) and raw.isdecimal(), "Invalid colleague output budget configuration")
    return validate_budget(int(raw))


def constructor(tree):
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "SemanticColleagueResponder"]
    require(len(calls) == 1, "Expected exactly one semantic responder constructor")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_runtime"]
    require(len(functions) == 1 and any(node is calls[0] for node in ast.walk(functions[0])),
            "Semantic responder constructor is outside the known runtime factory")
    assignments = [node for node in ast.walk(functions[0]) if isinstance(node, ast.Assign)
                   and node.value is calls[0] and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "semantic_responder"]
    require(len(assignments) == 1, "Semantic responder assignment has an unknown shape")
    return calls[0]


def patch_adapter(source, budget, expected_sha256):
    """Pure byte transformation; compile validates syntax without executing code."""
    validate_budget(budget)
    require(type(source) is bytes and 0 < len(source) <= 1_000_000, "Invalid private adapter source size")
    original_hash = digest(source)
    require(original_hash == expected_sha256, "Private adapter source differs from its frozen expected hash")
    tree = ast.parse(source, filename=ADAPTER)
    call = constructor(tree)
    require(not call.args, "Unexpected positional semantic responder arguments")
    expected_names = ["base_url", "api_key", "model", "effort", "timeout_seconds"]
    require([keyword.arg for keyword in call.keywords] == expected_names,
            "Unexpected semantic responder keyword shape or pre-existing budget")
    for keyword in call.keywords[:4]:
        require(isinstance(keyword.value, ast.Name) and keyword.value.id == keyword.arg,
                "Unexpected semantic responder configuration expression")
    timeout = call.keywords[-1].value
    require(isinstance(timeout, ast.Call) and isinstance(timeout.func, ast.Name)
            and timeout.func.id == "_timeout_seconds" and not timeout.args and not timeout.keywords,
            "Unexpected semantic responder timeout expression")
    lines = source.splitlines(keepends=True)
    closing = lines[call.end_lineno - 1]
    prefix = closing[:call.end_col_offset - 1]
    require(call.end_lineno > call.lineno and prefix.strip() == b""
            and closing[call.end_col_offset - 1:call.end_col_offset] == b")",
            "Unsupported semantic responder constructor layout")
    newline = b"\r\n" if closing.endswith(b"\r\n") else b"\n"
    offset = sum(len(line) for line in lines[:call.end_lineno - 1])
    insertion = prefix + b"    max_output_tokens=" + str(budget).encode("ascii") + b"," + newline
    patched = source[:offset] + insertion + source[offset:]
    changed_tree = ast.parse(patched, filename=ADAPTER)
    changed_call = constructor(changed_tree)
    added = changed_call.keywords.pop()
    require(added.arg == "max_output_tokens" and isinstance(added.value, ast.Constant)
            and added.value.value == budget, "Budget insertion could not be verified")
    require(ast.dump(tree, include_attributes=False) == ast.dump(changed_tree, include_attributes=False),
            "Adapter AST changed beyond the requested budget keyword")
    compile(patched, ADAPTER, "exec", dont_inherit=True)
    return patched, {"original_sha256": original_hash, "patched_sha256": digest(patched),
                     "max_output_tokens": budget, "constructor_line": call.lineno,
                     "ast_keyword_only": True, "compile_passed": True}


def notification_limit(tree):
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "request_model_json"]
    require(len(functions) == 1, "Expected one notification request factory")
    payloads = [node.value for node in functions[0].body if isinstance(node, ast.Assign)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "payload" and isinstance(node.value, ast.Dict)]
    require(len(payloads) == 1, "Unknown notification payload shape")
    payload = payloads[0]
    require(all(isinstance(key, ast.Constant) for key in payload.keys)
            and [key.value for key in payload.keys] ==
            ["model", "reasoning", "instructions", "input", "max_output_tokens", "stream"],
            "Unknown notification payload fields")
    return payload, 4


def patch_notification(source, budget, expected_sha256):
    validate_budget(budget)
    require(type(source) is bytes and 0 < len(source) <= 1_000_000, "Invalid notification source size")
    original_hash = digest(source)
    require(original_hash == expected_sha256, "Notification source differs from its frozen expected hash")
    tree = ast.parse(source, filename=NOTIFICATION)
    payload, index = notification_limit(tree)
    value = payload.values[index]
    require(isinstance(value, ast.Constant) and type(value.value) is int and value.value == 700,
            "Notification output budget has an unknown original value")
    lines = source.splitlines(keepends=True)
    begin = sum(len(line) for line in lines[:value.lineno - 1]) + value.col_offset
    end = sum(len(line) for line in lines[:value.end_lineno - 1]) + value.end_col_offset
    require(source[begin:end] == b"700", "Unknown notification budget literal")
    patched = source[:begin] + str(budget).encode("ascii") + source[end:]
    changed_tree = ast.parse(patched, filename=NOTIFICATION)
    changed_payload, changed_index = notification_limit(changed_tree)
    require(isinstance(changed_payload.values[changed_index], ast.Constant)
            and changed_payload.values[changed_index].value == budget, "Notification budget insertion failed")
    changed_payload.values[changed_index] = ast.Constant(value=700)
    require(ast.dump(tree, include_attributes=False) == ast.dump(changed_tree, include_attributes=False),
            "Notification AST changed beyond the budget literal")
    compile(patched, NOTIFICATION, "exec", dont_inherit=True)
    return patched, {"original_sha256": original_hash, "patched_sha256": digest(patched),
                     "max_output_tokens": budget, "payload_line": value.lineno,
                     "original_max_output_tokens": 700, "ast_budget_literal_only": True,
                     "compile_passed": True, "timeout_expressions_unchanged": True}


def require_created(docker, state, identifier, expected_image):
    require(state.get("phase") == "preparing" and state.get("kind") == "cowork"
            and state.get("containers", {}).get("workplace") == identifier,
            "Budget injection is restricted to this new CoWork sidecar")
    info = docker.owned(identifier, state["run_id"])
    labels = info.get("Config", {}).get("Labels") or {}
    require(info.get("Id") == identifier and labels.get(RUN_LABEL) == state["run_id"]
            and labels.get(ROLE_LABEL) == "workplace", "Sidecar ownership or role mismatch")
    lifecycle = info.get("State", {})
    require(lifecycle.get("Status") == "created" and not lifecycle.get("Running")
            and str(lifecycle.get("StartedAt", "")).startswith("0001-")
            and info.get("RestartCount", 0) == 0, "Refusing to change a sidecar that has already started")
    require(info["Config"].get("Image") == expected_image and "@sha256:" in expected_image,
            "Sidecar pinned image mismatch")
    return info


def verify_native_budget(docker, state, identifier, entry, save, budget, native):
    """Attest immutable environment-configured code before its first start."""
    info = require_created(docker, state, identifier, entry["images"]["workplace"])
    configured = dict(value.split("=", 1) for value in info["Config"].get("Env", []) if "=" in value)
    require(configured.get(BUDGET_ENV) == str(budget), "Native colleague budget was not passed to container")
    observed = {}
    with tempfile.TemporaryDirectory(prefix=".native-budget-", dir=state["root"]) as directory:
        for installed, expected in native["files"].items():
            destination = Path(directory) / Path(installed).name
            docker.call(["cp", identifier + ":" + installed, str(destination)])
            require(destination.is_file() and not destination.is_symlink(), "Native budget source is not a regular file")
            actual = digest(destination.read_bytes())
            require(actual == expected, "Native budget source differs from audited task revision")
            observed[installed] = actual
    require_created(docker, state, identifier, entry["images"]["workplace"])
    audit = {"schema": "cowork-sidecar-native-output-budget-v1", "status": "native-configuration-verified",
             "run_id": state["run_id"], "row": entry["row"], "task_version": entry["task_version"],
             "image": entry["images"]["workplace"], "before_first_start": True,
             "configuration_env": BUDGET_ENV, "max_output_tokens": budget,
             "installed_sha256": observed, "source_mutated": False, "credentials_exported": False}
    save(Path(state["root"]) / "sidecar-budget-patch.json", audit)
    return audit


def patch_created_sidecar(docker, state, identifier, entry, save, budget=None, prepared_adapter=None):
    """Patch, read back, and audit a stopped, never-started owned container."""
    budget = configured_budget() if budget is None else validate_budget(budget)
    native = NATIVE_CONFIGURED_BUDGETS.get(entry["row"])
    if native and entry.get("task_version") == native["task_version"]:
        return verify_native_budget(docker, state, identifier, entry, save, budget, native)
    expected = EXPECTED_ADAPTER_SHA256.get(entry["row"])
    expected_notification = EXPECTED_NOTIFICATION_SHA256.get(entry["row"])
    if prepared_adapter is not None:
        from public_sidecar_mount import PUBLIC_SHA256, verify_adapter_mount
        require(entry['row'] == 2, 'Prepared adapter is restricted to row 002')
        expected = PUBLIC_SHA256
    require(bool(expected) and bool(expected_notification), "No frozen backend hashes for this task")
    require_created(docker, state, identifier, entry["images"]["workplace"])
    audit = {"schema": "cowork-sidecar-output-budget-patch-v1", "at": time.time(),
             "run_id": state["run_id"], "row": entry["row"], "container_id": identifier,
             "image": entry["images"]["workplace"], "adapter_path": ADAPTER,
             "expected_source_sha256": expected, "max_output_tokens": budget,
             "semantic_max_output_tokens": budget, "notification_max_output_tokens": budget,
             "configuration_env": BUDGET_ENV, "before_first_start": True,
             "owner_packs_exported": False, "credentials_exported": False, "status": "preparing"}
    path = Path(state["root"]) / "sidecar-budget-patch.json"
    try:
        with tempfile.TemporaryDirectory(prefix=".sidecar-budget-", dir=state["root"]) as directory:
            prepared, audit["files"] = [], {}
            for backend_path, source_hash, patcher in (
                    (ADAPTER, expected, patch_adapter),
                    (NOTIFICATION, expected_notification, patch_notification)):
                if backend_path == ADAPTER and prepared_adapter is not None:
                    info = require_created(docker, state, identifier, entry['images']['workplace'])
                    details = verify_adapter_mount(info, state, entry, prepared_adapter, budget)
                    audit['files'][backend_path] = details
                    audit.update(details)
                    continue
                source_path = Path(directory) / Path(backend_path).name
                staged_path = Path(directory) / (Path(backend_path).name + ".patched")
                copied_path = Path(directory) / (Path(backend_path).name + ".readback")
                docker.call(["cp", identifier + ":" + backend_path, str(source_path)])
                require(source_path.is_file() and not source_path.is_symlink(), "Private backend must be a regular file")
                original = source_path.read_bytes()
                audit["files"][backend_path] = {"original_sha256": digest(original), "expected_sha256": source_hash}
                patched, details = patcher(original, budget, source_hash)
                audit["files"][backend_path].update(details)
                if backend_path == ADAPTER:
                    audit.update(details)
                # docker cp retains image file modes/ownership. Keep its
                # read-only source inode untouched and create our own staging file.
                with staged_path.open("xb") as staged:
                    os.fchmod(staged.fileno(), 0o600)
                    staged.write(patched)
                prepared.append((backend_path, staged_path, copied_path, patched))
            # Both transformations must validate before any container file changes.
            for backend_path, source_path, copied_path, patched in prepared:
                require_created(docker, state, identifier, entry["images"]["workplace"])
                docker.call(["cp", str(source_path), identifier + ":" + backend_path])
                docker.call(["cp", identifier + ":" + backend_path, str(copied_path)])
                require(copied_path.is_file() and not copied_path.is_symlink()
                        and copied_path.read_bytes() == patched, "Sidecar backend readback verification failed")
                audit["files"][backend_path]["readback_verified"] = True
            require_created(docker, state, identifier, entry["images"]["workplace"])
            if prepared_adapter is not None:
                info = require_created(docker, state, identifier, entry['images']['workplace'])
                verify_adapter_mount(info, state, entry, prepared_adapter, budget)
        audit.update(status="applied", completed_at=time.time(), readback_verified=True)
        save(path, audit)
        return audit
    except Exception as error:
        # No exception body: Docker errors must not accidentally export data.
        audit.update(status="failed", error_type=type(error).__name__, completed_at=time.time())
        save(path, audit)
        raise

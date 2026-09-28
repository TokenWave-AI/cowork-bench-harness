"""Repair only row 002's runtime timeout allocation before first sidecar start.

The source task, image, private knowledge and disclosure rules are unchanged.
This module is inert until called by a separate repair campaign's creation hook.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import time

SOURCE = "/usr/local/bin/semantic_colleague.py"
EXPECTED_SOURCE_SHA256 = "452fa5205ea2b1447b559c2316a37a2e84e58be7882ecc4bab408d0b152e280a"
POLICY = "whole-decision-remaining-attempts-v1"
RUN_LABEL = "org.cowork.release.run"
ROLE_LABEL = "org.cowork.release.role"


class SemanticTimeoutPatchError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SemanticTimeoutPatchError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def one(values, message):
    values = list(values)
    require(len(values) == 1, message)
    return values[0]


def function(body, name):
    return one((n for n in body if isinstance(n, ast.FunctionDef) and n.name == name),
               "Unknown semantic function layout: " + name)


def assignment(body, name):
    return one((n for n in body if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name),
               "Unknown budget assignment: " + name)


def key_index(node, key):
    require(isinstance(node, ast.Dict), "Unknown budget dictionary layout")
    return one((i for i, k in enumerate(node.keys)
                if isinstance(k, ast.Constant) and k.value == key),
               "Unknown budget dictionary field: " + key)


def budget_ast(original):
    """Construct the complete allowed AST change, without executing source."""
    tree = copy.deepcopy(original)
    cls = one((n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "SemanticColleagueResponder"), "Unknown semantic class")
    configuration = function(cls.body, "configuration")
    config = one((n.value for n in configuration.body if isinstance(n, ast.Return)),
                 "Unknown configuration return")
    config.values[key_index(config, "phase_budget_policy")] = ast.Constant(value=POLICY)
    decide = function(cls.body, "decide")
    decide.body.remove(assignment(decide.body, "phase_budget"))
    phase = function(decide.body, "run_phase")
    phase.body.remove(assignment(phase.body, "phase_deadline"))
    loop = one((n for n in phase.body if isinstance(n, ast.For)), "Unknown phase attempt loop")
    assignment(loop.body, "remaining").value = ast.parse("deadline - time.monotonic()", mode="eval").body
    loop.body.remove(assignment(loop.body, "attempt_timeout"))
    guard = one((n for n in loop.body if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "remaining <= 0"), "Unknown deadline guard")
    append = one((n for n in ast.walk(guard) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "error_codes.append"), "Unknown deadline error")
    require(len(append.args) == 1, "Unknown deadline error arguments")
    append.args[0] = ast.Constant(value="decision_deadline_exceeded")
    record = one((n.value for n in loop.body if isinstance(n, ast.AnnAssign)
                  and isinstance(n.target, ast.Name) and n.target.id == "record"),
                 "Unknown phase record")
    record.values[key_index(record, "timeout_seconds")] = ast.Name(id="remaining", ctx=ast.Load())
    index = key_index(record, "phase_budget_seconds")
    del record.keys[index]
    del record.values[index]
    call = one((n for n in ast.walk(loop) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "self.transport"), "Unknown semantic transport call")
    timeout = one((k for k in call.keywords if k.arg == "timeout_seconds"),
                  "Unknown semantic transport timeout")
    timeout.value = ast.Name(id="remaining", ctx=ast.Load())
    return ast.fix_missing_locations(tree)


EDITS = (
    ('"phase_budget_policy": "equal-reserved-phases-equal-remaining-attempts-v1",',
     '"phase_budget_policy": "' + POLICY + '",'),
    ('        phase_budget = self.timeout_seconds / (2 if hidden_ids else 1)\n', ''),
    ('            phase_deadline = min(deadline, time.monotonic() + phase_budget)\n', ''),
    ('                remaining = phase_deadline - time.monotonic()\n',
     '                remaining = deadline - time.monotonic()\n'),
    ('error_codes.append("phase_deadline_exceeded")', 'error_codes.append("decision_deadline_exceeded")'),
    ('                attempt_timeout = remaining / (self.attempts - attempt)\n', ''),
    ('"timeout_seconds": attempt_timeout,', '"timeout_seconds": remaining,'),
    ('                    "phase_budget_seconds": phase_budget,\n', ''),
    ('timeout_seconds=attempt_timeout, max_output_tokens=self.max_output_tokens,',
     'timeout_seconds=remaining, max_output_tokens=self.max_output_tokens,'),
)


def patch_source(source):
    """Apply nine exact budget edits to the single allowlisted source version."""
    require(type(source) is bytes and digest(source) == EXPECTED_SOURCE_SHA256,
            "Row 002 semantic source does not match its frozen expected hash")
    original = ast.parse(source, filename=SOURCE)
    patched = source
    for before, after in EDITS:
        old, new = before.encode(), after.encode()
        require(patched.count(old) == 1, "Unknown semantic budget source layout")
        patched = patched.replace(old, new, 1)
    changed = ast.parse(patched, filename=SOURCE)
    require(ast.dump(changed, include_attributes=False)
            == ast.dump(budget_ast(original), include_attributes=False),
            "Semantic AST changed outside the allowlisted budget locations")
    compile(patched, SOURCE, "exec", dont_inherit=True)
    return patched, {
        "original_sha256": digest(source), "patched_sha256": digest(patched),
        "budget_policy": POLICY, "exact_budget_edits": len(EDITS),
        "ast_budget_only": True, "compile_passed": True,
        "whole_decision_timeout_unchanged": True, "max_attempts_unchanged": True,
        "projection_validation_commit_logic_unchanged": True,
    }


def require_created(docker, state, identifier, entry):
    require(state.get("row") == 2 and entry.get("row") == 2,
            "Semantic timeout repair is restricted to row 002")
    require(state.get("phase") == "preparing" and state.get("kind") == "cowork"
            and state.get("containers", {}).get("workplace") == identifier,
            "Semantic timeout repair requires this new CoWork sidecar")
    info = docker.owned(identifier, state["run_id"])
    labels = info.get("Config", {}).get("Labels") or {}
    require(info.get("Id") == identifier and labels.get(RUN_LABEL) == state["run_id"]
            and labels.get(ROLE_LABEL) == "workplace", "Sidecar ownership or role mismatch")
    lifecycle = info.get("State", {})
    require(lifecycle.get("Status") == "created" and not lifecycle.get("Running")
            and str(lifecycle.get("StartedAt", "")).startswith("0001-")
            and info.get("RestartCount", 0) == 0,
            "Refusing to change a sidecar that has already started")
    expected = entry["images"]["workplace"]
    require(info["Config"].get("Image") == expected and "@sha256:" in expected,
            "Sidecar pinned image mismatch")


def patch_created_sidecar(docker, state, identifier, entry, save):
    """Copy only the semantic source; never inspect packs, prompts or credentials."""
    require_created(docker, state, identifier, entry)
    audit = {"schema": "cowork-semantic-timeout-patch-v1", "at": time.time(),
             "run_id": state["run_id"], "row": 2, "container_id": identifier,
             "source_path": SOURCE, "image": entry["images"]["workplace"],
             "expected_source_sha256": EXPECTED_SOURCE_SHA256,
             "before_first_start": True, "owner_packs_exported": False,
             "credentials_exported": False, "status": "preparing"}
    receipt = Path(state["root"]) / "semantic-timeout-patch.json"
    save(receipt, audit)
    try:
        with tempfile.TemporaryDirectory(prefix=".semantic-timeout-", dir=state["root"]) as directory:
            original = Path(directory) / "semantic_colleague.py"
            staged = Path(directory) / "semantic_colleague.py.patched"
            readback = Path(directory) / "semantic_colleague.py.readback"
            docker.call(["cp", identifier + ":" + SOURCE, str(original)])
            require(original.is_file() and not original.is_symlink(),
                    "Semantic backend must be a regular file")
            patched, details = patch_source(original.read_bytes())
            audit.update(details)
            with staged.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(patched)
            require_created(docker, state, identifier, entry)
            docker.call(["cp", str(staged), identifier + ":" + SOURCE])
            docker.call(["cp", identifier + ":" + SOURCE, str(readback)])
            require(readback.is_file() and not readback.is_symlink()
                    and readback.read_bytes() == patched, "Semantic backend readback verification failed")
            require_created(docker, state, identifier, entry)
        audit.update(status="applied", completed_at=time.time(), readback_verified=True)
        save(receipt, audit)
        return audit
    except Exception as error:
        audit.update(status="failed", error_type=type(error).__name__, completed_at=time.time())
        save(receipt, audit)
        raise


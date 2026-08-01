from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "audit_decisions", ROOT / "tools" / "audit_decisions.py")
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def expected_paths():
    return {f"commit-{index}": set(paths)
            for index, (_name, _subject, paths) in enumerate(audit.STACK_LAYERS, start=1)}


def expected_subjects():
    return {f"commit-{index}": subject
            for index, (_name, subject, _paths) in enumerate(audit.STACK_LAYERS, start=1)}


def validate(commits, paths, subjects):
    return audit.validate_stack_layers(
        commits, paths.__getitem__, subjects.__getitem__)


def test_stack_layer_check_accepts_the_derived_order_and_exact_boundaries():
    paths = expected_paths()
    subjects = expected_subjects()
    commits = list(paths)

    assert validate(commits, paths, subjects) == []


def test_stack_layer_check_rejects_a_reordered_layer_even_when_paths_match():
    paths = expected_paths()
    subjects = expected_subjects()
    commits = list(paths)
    commits[1], commits[2] = commits[2], commits[1]

    failures = validate(commits, paths, subjects)

    assert "layer 2 machine-readable JSON reads" in failures[0]
    assert "expected subject=" in failures[0]
    assert "layer 3 NDJSON lifecycle progress" in failures[1]


def test_stack_layer_check_rejects_a_layer_that_absorbs_another_path():
    paths = expected_paths()
    subjects = expected_subjects()
    commits = list(paths)
    paths[commits[0]].add("rc_repro/services/k8s.py")

    failures = validate(commits, paths, subjects)

    assert len(failures) == 1
    assert "actual paths=" in failures[0]


def test_current_checkout_has_the_curated_git_stack_without_local_stack_branches():
    checkout = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
        capture_output=True, text=True, check=False)
    if checkout.returncode:
        pytest.skip("source archives do not contain the Git history being audited")

    ok, covered, detail = audit.audit_stack_history(run_tip_tests=False)

    assert ok, detail
    assert covered.startswith("Git-derived stack commits=")
    assert "stack/" not in detail

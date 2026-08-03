#!/usr/bin/env python3
"""Audit the Wayfinder decisions against what the code actually does.

Written because twelve decisions were closed with no acceptance evidence and
implementation then found fourteen defects. The closure contract
(Canepro/rc-repro#14) requires a *derived* enumeration and a *repeatable* check, so
this file is the mechanism: it derives each covered set from the source rather than
restating a hand-written list, and prints PASS or FAIL per decision.

Run:  python3 tools/audit_decisions.py [--verbose]
Exit: 0 if every decision holds, 1 otherwise (so CI can gate on it).

A check here is deliberately shallow: it asserts the decision's *mechanism exists
and is reachable*, not that it is bug-free. That is the level at which the fourteen
defects lived — the decision was right and nothing had carried it out.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


# --- derivations: every covered set comes from the source ----------------------

def cli_commands() -> dict[str, str]:
    """{command name: body}, derived from the typer registrations."""
    src = read("rc_repro/cli.py")
    marks = sorted((m.start(), m.group(2), m.group(1))
                   for m in re.finditer(r'@app\.command\(([^)]*)\)\s*\ndef (\w+)\(', src))
    out: dict[str, str] = {}
    for i, (start, fn, decl) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        name = fn
        m = re.search(r'name\s*=\s*"([^"]+)"', decl)
        if m:
            name = m.group(1)
        out[name.replace("_cmd", "")] = src[start:end]
    return out


def commands_with_json() -> set[str]:
    # substring, not an exact literal: `--json/--no-json` is still a --json surface,
    # and an exact match silently under-reports (it did on the first attempt).
    return {n for n, body in cli_commands().items() if "--json" in body}


def lifecycle_service_functions() -> set[str]:
    return set(re.findall(r"^def (\w+)\(", read("rc_repro/services/lifecycle.py"), re.M))


def k8s_service_functions() -> set[str]:
    return set(re.findall(r"^def (\w+)\(", read("rc_repro/services/k8s.py"), re.M))


def git_text(*args: str) -> str:
    """Run a read-only Git query and return stdout, or raise with its real cause."""
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                            text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def audit_tip() -> str:
    """Return the contribution tip, ignoring a base-reconciliation merge commit."""
    merges = git_text("rev-list", "--first-parent", "--merges", "-n", "1", "HEAD")
    if merges:
        fields = git_text("rev-list", "--parents", "-n", "1", merges).split()
        # A PR branch may merge the moving upstream main into itself to become
        # mergeable again. Its first parent remains the contribution stack; the
        # merge commit and any review-only commits after it are synchronisation
        # metadata, not one of the audited layers.
        return fields[1]
    return git_text("rev-parse", "HEAD")


def stack_base() -> tuple[str, str]:
    """Find the closest main ref that is an ancestor of the checked-out stack."""
    tip = audit_tip()
    refs = git_text("for-each-ref", "--format=%(refname)", "refs/heads/main",
                    "refs/remotes").splitlines()
    main_refs = sorted({ref for ref in refs
                        if ref == "refs/heads/main" or ref.endswith("/main")})
    candidates: list[tuple[int, str, str]] = []
    for ref in main_refs:
        merge_base = git_text("merge-base", tip, ref)
        count = int(git_text("rev-list", "--count", f"{merge_base}..{tip}"))
        if count:
            candidates.append((count, ref, merge_base))
    if not candidates:
        raise RuntimeError("no main ref with stack commits is available; fetch main history")
    _count, ref, merge_base = min(candidates)
    return ref, merge_base


def stack_commits(base: str) -> list[str]:
    out = git_text("rev-list", "--reverse", "--topo-order", f"{base}..{audit_tip()}")
    return out.splitlines() if out else []


def commit_paths(commit: str) -> set[str]:
    out = git_text("diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    return set(out.splitlines()) if out else set()


def commit_subject(commit: str) -> str:
    return git_text("show", "-s", "--format=%s", commit)


# The revised answer to decision #10 specified nine independently mergeable PR
# milestones. Each tuple records the milestone's subject and exact changed-file
# boundary. Tests deliberately travel with the behavior they prove. Commit ids are
# never listed: the covered commits always come from the checked-out Git ancestry.
STACK_LAYERS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("error taxonomy and exit codes",
     "feat: give domain errors stable codes and real exit codes", frozenset({
         "rc_repro/cli.py", "rc_repro/errors.py", "rc_repro/services/lifecycle.py",
         "rc_repro/ui.py", "tests/test_core.py", "tests/test_services.py",
     })),
    ("machine-readable JSON reads",
     "feat: add the machine-readable output envelope and --json read verbs", frozenset({
         "rc_repro/cli.py", "rc_repro/jsonout.py", "tests/test_core.py",
     })),
    ("NDJSON lifecycle progress",
     "feat: stream NDJSON progress and add --json to the lifecycle verbs", frozenset({
         "rc_repro/cli.py", "rc_repro/jsonout.py", "tests/test_core.py",
     })),
    ("capability discovery",
     "feat: add capabilities as the contract's self-description", frozenset({
         "rc_repro/cli.py", "rc_repro/jsonout.py", "tests/test_core.py",
     })),
    ("Kubernetes microservices preset",
     "feat: give Kubernetes repros info and logs parity", frozenset({
         "rc_repro/data/presets/microservices.yaml", "rc_repro/jsonout.py",
         "rc_repro/presets/__init__.py", "rc_repro/runner.py",
         "rc_repro/services/k8s.py", "rc_repro/services/lifecycle.py",
         "tests/test_core.py", "tests/test_services.py",
     })),
    ("one-time onboarding",
     "feat: add one-time onboarding and persisted grants", frozenset({
         "rc_repro/cli.py", "rc_repro/jsonout.py",
         "rc_repro/services/onboarding.py", "tests/test_core.py",
     })),
    ("canonical agent skill",
     "feat: ship the canonical agent skill and install it per host", frozenset({
         ".agents/skills/rc-repro/SKILL.md", ".claude/skills/rc-repro/SKILL.md",
         "pyproject.toml", "rc_repro/cli.py", "rc_repro/data/skill/SKILL.md",
         "rc_repro/jsonout.py", "rc_repro/services/skill.py", "tests/test_core.py",
     })),
    ("backend-neutral evidence",
     "feat: add secret-safe, backend-neutral evidence", frozenset({
         "rc_repro/cli.py", "rc_repro/services/evidence.py", "tests/test_services.py",
     })),
    ("capacity and readiness",
     "feat: enforce the measured capacity floor and wire Kubernetes readiness", frozenset({
         "rc_repro/cli.py", "rc_repro/runner.py", "rc_repro/services/k8s.py",
         "tests/test_services.py",
     })),
)


def validate_stack_layers(commits: list[str], paths_for=commit_paths,
                          subject_for=commit_subject) -> list[str]:
    """Return precise order or boundary failures for the nine PR milestones."""
    failures: list[str] = []
    if len(commits) < len(STACK_LAYERS):
        return [f"only {len(commits)} commits; expected {len(STACK_LAYERS)} layers"]
    for index, ((name, expected_subject, expected_paths), commit) in enumerate(
            zip(STACK_LAYERS, commits, strict=False), start=1):
        subject = subject_for(commit)
        if subject != expected_subject:
            failures.append(
                f"layer {index} {name}: expected subject={expected_subject!r} "
                f"actual subject={subject!r}")
        actual_paths = paths_for(commit)
        if actual_paths != set(expected_paths):
            failures.append(
                f"layer {index} {name}: expected paths={sorted(expected_paths)} "
                f"actual paths={sorted(actual_paths)}")
    return failures


def validate_linear_history(base: str, commits: list[str]) -> list[str]:
    failures: list[str] = []
    expected_parent = base
    for commit in commits:
        fields = git_text("rev-list", "--parents", "-n", "1", commit).split()
        parents = fields[1:]
        if parents != [expected_parent]:
            failures.append(
                f"{commit[:8]} parents={parents} expected only {expected_parent[:8]}")
        expected_parent = commit
    return failures


def test_stack_tips(commits: list[str]) -> list[str]:
    """Run every contribution commit's own tests from a disposable source archive."""
    failures: list[str] = []

    def extract_archive(tar: tarfile.TarFile, destination: str) -> None:
        """Extract a Git archive safely on both Python 3.11 and 3.12+."""
        try:
            # Python 3.12+ provides the traversal-safe data filter.
            tar.extractall(destination, filter="data")
            return
        except TypeError:
            # Python 3.11 has no ``filter`` parameter. Apply the same restrictions
            # explicitly rather than falling back to an unfiltered extraction.
            root = Path(destination).resolve()
            for member in tar.getmembers():
                target = (root / member.name).resolve()
                if target != root and root not in target.parents:
                    raise ValueError(f"archive member escapes destination: {member.name}")
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise ValueError(f"unsupported archive member: {member.name}")
                tar.extract(member, destination)

    for index, commit in enumerate(commits):
        name = (STACK_LAYERS[index][0] if index < len(STACK_LAYERS)
                else commit_subject(commit))
        archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=ROOT,
                                 capture_output=True, check=False)
        if archive.returncode:
            failures.append(f"{commit[:8]} {name}: git archive failed")
            continue
        with tempfile.TemporaryDirectory(prefix="rc-repro-stack-audit-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                extract_archive(tar, tmp)
            result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=tmp,
                                    capture_output=True, text=True, check=False)
        if result.returncode:
            output = "\n".join((result.stdout + result.stderr).splitlines()[-12:])
            failures.append(f"{commit[:8]} {name}: tests failed\n{output}")
    return failures


def audit_stack_history(*, run_tip_tests: bool = True):
    ref, base = stack_base()
    commits = stack_commits(base)
    curated = commits[:len(STACK_LAYERS)]
    failures = validate_stack_layers(commits)
    failures.extend(validate_linear_history(base, commits))
    if run_tip_tests and not failures:
        failures.extend(test_stack_tips(commits))
    covered = [f"{commit[:8]}:{name}" for commit, (name, _subject, _paths)
               in zip(curated, STACK_LAYERS, strict=False)]
    followups = [commit[:8] for commit in commits[len(STACK_LAYERS):]]
    detail = (f"base={ref}@{base[:8]} curated={covered} followups={followups} "
              f"tip_tests={'run' if run_tip_tests else 'skipped'} failures={failures}")
    return not failures, f"Git-derived stack commits={len(commits)}", detail


# --- the decisions -------------------------------------------------------------
# Each returns (ok, covered_set_description, detail).

def d2_official_chart():
    k8s = read("rc_repro/services/k8s.py")
    pins = "--version" in k8s and "resolve_chart_version" in k8s
    official = "rocketchat.github.io/helm-charts" in k8s
    vendored = bool(list((ROOT / "rc_repro").rglob("Chart.yaml")))
    ok = pins and official and not vendored
    return ok, "chart source + pinning", (
        f"official repo={official} pinned={pins} vendored_manifests={vendored}")


def d3_skill_hosts():
    skill = read("rc_repro/services/skill.py")
    hosts = set(re.findall(r'^\s*"(\w+)":\s*\{"user"', skill, re.M))
    covered = set(re.findall(r'"(\w+)":\s*"(?:claude|codex)"', skill))
    expected = {"claude", "codex", "cursor", "copilot"}
    ok = expected <= (hosts | covered)
    return ok, f"hosts={sorted(hosts | covered)}", f"missing={sorted(expected - (hosts | covered))}"


def d4_local_runtime():
    k8s = read("rc_repro/services/k8s.py")
    needed = ["kind", "CLUSTER_NAME", "namespace_for", "OWNER_LABEL",
              "FLOOR_MEMORY_GIB", "FLOOR_CPUS", "check_capacity", "owns_namespace"]
    missing = [n for n in needed if n not in k8s]
    return not missing, "runtime mechanisms", f"missing={missing}"


#: The parity table from design/kubernetes-lifecycle-integration.md. This list is the
#: one thing here that is transcribed rather than derived, so it is named as such:
#: the decision's own table is the specification being audited.
PARITY_VERBS = ["up", "ready", "list", "info", "logs", "exec", "evidence", "down",
                "prune", "restart", "doctor"]


#: Where each parity verb's Kubernetes path must appear. Transcribed from the design's
#: table (the spec being audited) and mapped to the function that actually implements
#: it, because the CLI verb and the service function rarely share a name: `up` is
#: create_repro, `down` is teardown, `info` is detail.
#:
#: This mapping exists because the first two versions of this check were both wrong in
#: opposite directions: matching the word "Kubernetes" passed `doctor` on a comment,
#: and matching the verb name against service functions reported seven false failures.
#: A check has to be calibrated before its verdict means anything.
PARITY_SITES: dict[str, tuple[str, str]] = {
    "up":       ("rc_repro/services/lifecycle.py", "create_repro"),
    "ready":    ("rc_repro/services/lifecycle.py", "wait_and_finalize"),
    "list":     ("rc_repro/services/lifecycle.py", "list_repros"),
    "info":     ("rc_repro/services/lifecycle.py", "detail"),
    "down":     ("rc_repro/services/lifecycle.py", "teardown"),
    "prune":    ("rc_repro/services/lifecycle.py", "prunable"),
    "restart":  ("rc_repro/services/lifecycle.py", "set_state"),
    "evidence": ("rc_repro/services/evidence.py", "record"),
    "logs":     ("rc_repro/cli.py", "logs"),
    "doctor":   ("rc_repro/cli.py", "doctor"),
    "exec":     ("rc_repro/services/k8s.py", "exec_in"),
}

#: A dispatch is a call into the Kubernetes module (`_k8s` is an accepted alias) or
#: into one of the topology-aware helpers, which are the shared seam a verb uses when
#: it needs reachability or a refusal rather than a Kubernetes operation.
_DISPATCH = re.compile(r"\b_?k8s(?:svc)?\.\w+\(|"
                       r"\bensure_reachable\(|\brequire_compose_topology\(|"
                       r"\btopology_of_repro\(")


def _function_body(rel: str, fn: str) -> str:
    r"""The function's source, terminated only at a COLUMN-ZERO def or decorator.

    `\n\s*def` would stop at a nested helper: it truncated `doctor` at its inner
    `def line(...)`, cutting off the Kubernetes section and reporting a false failure.
    """
    src = read(rel)
    m = re.search(rf"\ndef {fn}\(.*?(?=\n(?:@|def )\w|\Z)", src, re.S)
    return m.group(0) if m else ""


def d5_lifecycle_parity():
    missing = []
    for verb, (rel, fn) in PARITY_SITES.items():
        body = _function_body(rel, fn)
        if not body:
            missing.append(f"{verb} (no {fn} in {rel})")
            continue
        # exec_in IS the Kubernetes implementation, so its own presence is the path.
        if rel.endswith("k8s.py") or _DISPATCH.search(body):
            continue
        missing.append(f"{verb} ({fn})")
    return not missing, f"parity verbs={sorted(PARITY_SITES)}", \
        f"no Kubernetes dispatch={missing}"


def k8s_src() -> str:
    return read("rc_repro/services/k8s.py")


#: Verbs the contract decision named as the machine-driven lifecycle. Derived from
#: what an agent must do end to end, not from the whole CLI: the decision covered the
#: lifecycle, and saying so is the stated boundary.
CONTRACT_VERBS = ["up", "ready", "down", "list", "info", "evidence", "doctor",
                  "capabilities"]


def d6_json_contract():
    withj = commands_with_json()
    jsonout = read("rc_repro/jsonout.py")
    envelope = all(k in jsonout for k in ('"schema"', '"contract"', '"ok"', '"warnings"'))
    # capabilities is JSON by definition and takes no flag, by decision.
    missing = [v for v in CONTRACT_VERBS if v not in withj and v != "capabilities"]
    return (envelope and not missing), f"contract verbs={CONTRACT_VERBS}", \
        f"envelope={envelope} without --json={missing}"


def d7_onboarding_enforced():
    src = "".join(read(f"rc_repro/{p}") for p in
                  ("cli.py", "services/lifecycle.py", "services/k8s.py", "web/app.py"))
    calls_gate = "require_onboarded(" in src
    calls_grant = "require_grant(" in src or "grants" in read("rc_repro/services/k8s.py")
    return (calls_gate and calls_grant), "onboarding gate reachable", \
        f"require_onboarded called={calls_gate} grant consulted={calls_grant}"


def d8_skill_copies_match():
    canonical = read("rc_repro/data/skill/SKILL.md")
    copies = {rel: read(rel) for rel in
              (".claude/skills/rc-repro/SKILL.md", ".agents/skills/rc-repro/SKILL.md")}
    bad = [rel for rel, text in copies.items() if text != canonical or not text]
    return (bool(canonical) and not bad), f"committed copies={sorted(copies)}", \
        f"drifted={bad}"


def d9_evidence():
    ev = read("rc_repro/services/evidence.py")
    k8s = k8s_src()
    needed = ["safe_origin", '"ownership"', '"license"', '"retention"', '"cleanup"']
    missing = [n for n in needed if n not in ev]
    residual = '"residual"' in k8s
    return (not missing and residual), "evidence fields", \
        f"missing={missing} teardown_residual={residual}"


def d10_stack_exists():
    return audit_stack_history()


#: The terminal conditions the fail-fast decision named. Transcribed from that
#: decision, which is the specification being audited.
TERMINAL_CONDITIONS = {
    "platform mismatch": ["no match for platform", "platform"],
    "registry auth": ["denied", "unauthorized"],
    "mongo kernel": ["SERVER-121912"],
    "replica set": ["change streams"],
}


def d11_fail_fast():
    k8s = k8s_src()
    missing = [name for name, needles in TERMINAL_CONDITIONS.items()
               if not any(n in k8s for n in needles)]
    return not missing, f"terminal conditions={sorted(TERMINAL_CONDITIONS)}", \
        f"unclassified={missing}"


def d12_floor():
    k8s = k8s_src()
    ok = "FLOOR_MEMORY_GIB = 6.0" in k8s and "FLOOR_CPUS = 4" in k8s
    return ok, "measured floor constants", f"match measurement={ok}"


def d13_licence_gate():
    # The warning lives in the shared create path (lifecycle), before topology
    # dispatch, so it fires for every EE preset. Earlier this check read only
    # cli/k8s and missed it: a check looking in the wrong file is a false FAIL, the
    # mirror of the false PASS the contract warns about.
    src = read("rc_repro/services/lifecycle.py")
    warns = "LICENSE_ABSENT_EE_PRESET" in src and "warn_if_unlicensed" in src
    reachable = "warn_if_unlicensed(req, emit)" in src
    in_evidence = '"license"' in read("rc_repro/services/evidence.py")
    return (warns and reachable and in_evidence), "licence signalling", \
        f"warning_defined={warns} called_in_create={reachable} in_evidence={in_evidence}"


DECISIONS = [
    (2, "Official Rocket.Chat Kubernetes artifact and compatibility", d2_official_chart),
    (3, "Portable agent-skill format and adapters", d3_skill_hosts),
    (4, "rc-repro-owned local Kubernetes runtime", d4_local_runtime),
    (5, "How Kubernetes joins the lifecycle", d5_lifecycle_parity),
    (6, "Stable agent-facing CLI and JSON contract", d6_json_contract),
    (7, "One-time onboarding and persisted preferences", d7_onboarding_enforced),
    (8, "Canonical rc-repro skill and host adapters", d8_skill_copies_match),
    (9, "Backend-neutral evidence, retention and ownership", d9_evidence),
    (10, "Incremental implementation and review sequence", d10_stack_exists),
    (11, "Terminal-versus-transient failure classification", d11_fail_fast),
    (12, "Microservices footprint floor", d12_floor),
    (13, "Enterprise licence gate", d13_licence_gate),
]


def main() -> int:
    verbose = "--verbose" in sys.argv
    failures = []
    print(f"{'':4} {'DECISION':52} VERDICT")
    for num, title, check in DECISIONS:
        try:
            ok, covered, detail = check()
        except Exception as exc:  # noqa: BLE001 - a broken check is a failure
            ok, covered, detail = False, "check raised", f"{type(exc).__name__}: {exc}"
        print(f"#{num:<3} {title[:52]:52} {'PASS' if ok else 'FAIL'}")
        if verbose or not ok:
            print(f"       covered: {covered}")
            print(f"       detail : {detail}")
        if not ok:
            failures.append(num)
    print()
    if failures:
        print(f"{len(failures)} decision(s) do not hold: "
              + ", ".join(f"#{n}" for n in failures))
        return 1
    print("every decision holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

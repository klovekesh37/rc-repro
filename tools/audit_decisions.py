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

import re
import subprocess
import sys
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


def git_branches() -> set[str]:
    try:
        out = subprocess.run(["git", "branch", "--list", "stack/*"], cwd=ROOT,
                             capture_output=True, text=True, check=False).stdout
    except OSError:
        return set()
    return {ln.strip().lstrip("* ").strip() for ln in out.splitlines() if ln.strip()}


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
    """The function's source, terminated only at a COLUMN-ZERO def or decorator.

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
    branches = git_branches()
    return bool(branches), f"stack branches={len(branches)}", f"{sorted(branches)[:3]}..."


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
    src = read("rc_repro/cli.py") + read("rc_repro/services/k8s.py")
    warns = "LICENSE_ABSENT" in src
    in_evidence = '"license"' in read("rc_repro/services/evidence.py")
    return (warns and in_evidence), "licence signalling", \
        f"warning_at_create={warns} recorded_in_evidence={in_evidence}"


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

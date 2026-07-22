"""Load-test service for the web GUI (a focused subset of the CLI `loadtest`).

Supports the common options — scenario, vus, duration, users, slo, constrain,
diag, stats, live — with the SAME safety-critical restore-in-`finally` as the CLI
(rate limiter, Prometheus setting, resource caps, and the users.json token file
are always restored/cleaned up, even on failure). Returns a structured result.

The full-featured CLI `loadtest` (ramp/spike/custom/webhook/save/compare/report)
is unchanged; unifying the two behind this service is a planned follow-up.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict as dc_asdict
from dataclasses import dataclass

import requests

from rc_repro import config, rcapi, runner
from rc_repro import seed as seeder
from rc_repro.errors import DockerError, NotReadyError, ValidationError
from rc_repro.services import lifecycle
from rc_repro.services.events import Emit, info, null_emit, warn

_GUI_SCENARIOS = ("messages", "login", "read", "mixed", "journey", "badbot")


@dataclass
class LoadtestReq:
    name: str
    scenario: str = "messages"
    vus: int = 10
    duration: str = "30s"
    users_n: int = 10
    slo: str = ""
    constrain: str = ""
    diag: bool = True
    stats: bool = False
    live: bool = False
    slowms: int = 100


def _rc_services_in(doc: dict) -> list[str]:
    return [s for s in doc.get("services", {}) if s == "rocketchat" or s.startswith("rocketchat-")]


def _loadtest_target(doc: dict) -> str:
    svcs = doc.get("services", {})
    if "traefik" in svcs:
        return "http://traefik:80"
    rc = _rc_services_in(doc)
    if "rocketchat" in rc:
        return "http://rocketchat:3000"
    return f"http://{rc[0]}:3000" if rc else "http://rocketchat:3000"


def _short(full: str, repro_name: str) -> str:
    prefix = f"{config.PROJECT_PREFIX}{repro_name}-"
    s = full[len(prefix):] if full.startswith(prefix) else full
    return re.sub(r"-\d+$", "", s)


def _login_seed_users(root_url: str, count: int) -> list[dict]:
    users: list[dict] = []
    url = root_url.rstrip("/") + "/api/v1/login"
    for i in range(count):
        uname = seeder.username(i)
        try:
            r = requests.post(url, json={"user": uname, "password": uname}, timeout=10)
        except requests.RequestException:
            break
        if r.status_code == 200:
            d = r.json().get("data") or {}
            if d.get("authToken"):
                users.append({"username": uname, "password": uname,
                              "token": d["authToken"], "uid": d["userId"]})
    return users


def run_loadtest(req: LoadtestReq, emit: Emit = null_emit) -> dict:
    from rc_repro import monitoring, perf
    from rc_repro.perf import (constrain as constrain_mod, k6, mongoprof, rcmetrics,
                               slo as slo_mod, timeline as timeline_mod, verdict as verdict_mod)

    lifecycle.require_docker()
    if req.scenario not in _GUI_SCENARIOS:
        raise ValidationError(f"scenario must be one of {', '.join(_GUI_SCENARIOS)}")
    if req.vus < 1:
        raise ValidationError("vus must be >= 1")

    m = runner.read_meta(lifecycle.resolve_name(req.name))
    doc = runner.read_compose(m.name)
    target = _loadtest_target(doc)
    rc_services = _rc_services_in(doc) or ["rocketchat"]

    if req.live:
        if not (isinstance(m.extra, dict) and m.extra.get("monitoring")):
            raise ValidationError("live streaming needs monitoring attached first")
        prom_cmd = (doc.get("services", {}).get("prometheus") or {}).get("command", [])
        if not any("remote-write-receiver" in str(c) for c in prom_cmd):
            raise ValidationError("live needs Prometheus with remote-write - re-attach monitoring")

    try:
        rules = slo_mod.parse(req.slo) if req.slo else []
    except ValueError as exc:
        raise ValidationError(f"bad slo: {exc}") from exc
    per_service = {}
    if req.constrain:
        try:
            per_service = constrain_mod.resolve_services(
                constrain_mod.parse(req.constrain), list(doc.get("services", {})))
        except ValueError as exc:
            raise ValidationError(f"bad constrain: {exc}") from exc

    try:
        auth = lifecycle.login(m)
        token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD,
                                   token_name="rc-repro-loadtest", bypass_2fa=True)
    except Exception as exc:  # noqa: BLE001
        raise NotReadyError(f"could not authenticate (ready?): {exc}") from exc

    users = _login_seed_users(m.root_url, req.users_n) if req.users_n > 0 else []

    applied_constraints: list = []
    limiter_was_off = True
    metrics_changed, mongo_prior, sampler, mon = False, None, None, None
    resources = summary = None
    rcm_report: dict = {}
    since_ms = int(time.time() * 1000)
    try:
        if per_service:
            try:
                applied_constraints = constrain_mod.apply(m.name, per_service)
            except RuntimeError as exc:
                raise DockerError(f"could not apply constrain: {exc}") from exc
            info(emit, f"constrained: {constrain_mod.human(per_service)} (restored after)", phase="k6")
        limiter_was_off = rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                            config.RC_RATE_LIMITER_SETTING) is False
        if not limiter_was_off:
            rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                              config.RC_RATE_LIMITER_SETTING, False)
        if req.diag:
            if rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                 monitoring.RC_METRICS_SETTING) is not True:
                metrics_changed = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                                    monitoring.RC_METRICS_SETTING, True)
            mongo_prior = mongoprof.start(m.name, req.slowms)
        mon = perf.ResourceMonitor(m.name).start() if req.stats else None
        since_ms = int(time.time() * 1000)
        if req.diag:
            sampler = rcmetrics.RCMetricsSampler(m.name, rc_services).start()
        info(emit, f"running k6: {req.scenario} @ {req.vus} VUs for {req.duration} -> {target}"
                   f" ({'%d seeded users' % len(users) if users else 'admin token'})", phase="k6")
        summary = k6.run(m.name, req.scenario, vus=req.vus, duration=req.duration, ramp=None,
                         token=token, uid=auth.user_id, target=target, users=users or None,
                         quiet=True, timeline=req.diag, spike=None, live=req.live)
    except RuntimeError as exc:
        raise DockerError(str(exc)) from exc
    finally:
        if sampler:
            rcm_report = sampler.stop()
        if mon:
            resources = mon.stop()
        if not limiter_was_off:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  config.RC_RATE_LIMITER_SETTING, True)
            except Exception:  # noqa: BLE001
                warn(emit, "could not restore the API rate limiter setting", phase="restore")
        if metrics_changed:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  monitoring.RC_METRICS_SETTING, False)
            except Exception:  # noqa: BLE001
                warn(emit, "could not restore the Prometheus metrics setting", phase="restore")
        if mongo_prior:
            mongoprof.stop(m.name, mongo_prior)
        for problem in constrain_mod.restore(applied_constraints):
            warn(emit, f"could not restore resource limits - {problem}", phase="restore")
        (runner.workspace(m.name) / "loadtest" / "users.json").unlink(missing_ok=True)

    mongo_slow = mongoprof.collect(m.name, since_ms) if (req.diag and mongo_prior) else None
    tl = None
    if req.diag:
        points = runner.workspace(m.name) / "loadtest" / "points.json"
        tl = timeline_mod.parse(points)
        points.unlink(missing_ok=True)

    slo_results = slo_mod.evaluate(rules, summary) if rules else []
    short_stats = {_short(k, m.name): v for k, v in resources.items()} if resources else None
    verdict_lines = (verdict_mod.analyze(summary, rcmetrics=rcm_report or None, mongo=mongo_slow,
                                         resources=short_stats, timeline=tl)
                     if req.diag else [])
    passed = (not slo_results) or all(r["ok"] for r in slo_results)

    grafana = f"http://localhost:{config.MONITOR_PORTS[1]}" if req.live else None
    info(emit, f"done: p95 {summary.get('p95', 0):.0f}ms at {summary.get('rps', 0):.1f} req/s, "
               f"{summary.get('error_rate', 0) * 100:.2f}% errors"
               + ("" if passed else " - SLO FAIL"), phase="done",
         data={"passed": passed})
    return {
        "ctx": {"name": m.name, "scenario": req.scenario, "vus": req.vus,
                "duration": req.duration, "users": len(users), "target": target,
                "constrained": constrain_mod.human(per_service) if per_service else ""},
        "summary": summary, "slo": slo_results, "passed": passed,
        "verdict": verdict_lines,
        "diag": {"rcmetrics": rcm_report, "mongo": mongo_slow, "timeline": tl} if req.diag else None,
        "resources": {k: dc_asdict(v) for k, v in short_stats.items()} if short_stats else None,
        "grafana_url": grafana,
    }


# --- capacity ----------------------------------------------------------------

@dataclass
class CapacityReq:
    name: str
    scenario: str = "journey"
    users_n: int = 10
    slo: str = "p95=500ms,error=2%"
    start: int = 10
    max_vus: int = 640
    step_duration: str = "20s"
    constrain: str = ""


def run_capacity(req: CapacityReq, emit: Emit = null_emit) -> dict:
    from rc_repro import monitoring
    from rc_repro.perf import constrain as constrain_mod, k6, rcmetrics, slo as slo_mod

    lifecycle.require_docker()
    if req.scenario not in _GUI_SCENARIOS:
        raise ValidationError(f"scenario must be one of {', '.join(_GUI_SCENARIOS)}")
    try:
        rules = slo_mod.parse(req.slo)
    except ValueError as exc:
        raise ValidationError(f"bad slo: {exc}") from exc
    if req.start < 1 or req.max_vus < req.start:
        raise ValidationError("start must be >= 1 and max >= start")

    m = runner.read_meta(lifecycle.resolve_name(req.name))
    doc = runner.read_compose(m.name)
    target = _loadtest_target(doc)
    rc_services = _rc_services_in(doc) or ["rocketchat"]
    per_service = {}
    if req.constrain:
        try:
            per_service = constrain_mod.resolve_services(
                constrain_mod.parse(req.constrain), list(doc.get("services", {})))
        except ValueError as exc:
            raise ValidationError(f"bad constrain: {exc}") from exc
    try:
        auth = lifecycle.login(m)
        token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD,
                                   token_name="rc-repro-loadtest", bypass_2fa=True)
    except Exception as exc:  # noqa: BLE001
        raise NotReadyError(f"could not authenticate (ready?): {exc}") from exc
    users = _login_seed_users(m.root_url, req.users_n) if req.users_n > 0 else []

    applied: list = []
    limiter_was_off = True
    metrics_changed = False
    steps: list[dict] = []
    last_pass = first_fail = None

    def run_step(n: int, tag: str = "") -> dict:
        sampler = rcmetrics.RCMetricsSampler(m.name, rc_services).start()
        try:
            s = k6.run(m.name, req.scenario, vus=n, duration=req.step_duration, ramp=None,
                       token=token, uid=auth.user_id, target=target, users=users or None, quiet=True)
        finally:
            rcm = sampler.stop()
        res = slo_mod.evaluate(rules, s)
        lag_max = 0.0
        for sm in rcm.values():
            lag = sm.get("eventloop_lag_max_s") or sm.get("eventloop_lag_s")
            if lag:
                lag_max = max(lag_max, lag["max"])
        row = {"vus": n, "rps": s.get("rps", 0.0), "p95": s.get("p95", 0.0),
               "error_rate": s.get("error_rate", 0.0), "ok": all(r["ok"] for r in res),
               "lag_max_s": lag_max,
               "breached": [f"{r['key']} {r['op']} {r['raw']} (actual {slo_mod.fmt_actual(r['key'], r['actual'])})"
                            for r in res if not r["ok"]]}
        steps.append(row)
        info(emit, f"{n} VUs{tag}: {row['rps']:.1f} req/s, p95 {row['p95']:.0f}ms, "
                   f"{'PASS' if row['ok'] else 'FAIL'}", phase="k6")
        return row

    try:
        if per_service:
            try:
                applied = constrain_mod.apply(m.name, per_service)
            except RuntimeError as exc:
                raise DockerError(f"could not apply constrain: {exc}") from exc
        limiter_was_off = rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                            config.RC_RATE_LIMITER_SETTING) is False
        if not limiter_was_off:
            rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD, config.RC_RATE_LIMITER_SETTING, False)
        if rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD, monitoring.RC_METRICS_SETTING) is not True:
            metrics_changed = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                                monitoring.RC_METRICS_SETTING, True)
        info(emit, f"capacity search: {req.scenario}, SLO {req.slo}, steps of {req.step_duration}", phase="k6")
        n = req.start
        while n <= req.max_vus:
            row = run_step(n)
            if row["ok"]:
                last_pass = n
                n *= 2
            else:
                first_fail = n
                break
        if first_fail and last_pass:
            lo, hi = last_pass, first_fail
            for _ in range(2):
                mid = (lo + hi) // 2
                if mid <= lo or mid >= hi:
                    break
                row = run_step(mid, tag=" (bisect)")
                if row["ok"]:
                    lo = last_pass = mid
                else:
                    hi = first_fail = mid
    finally:
        if not limiter_was_off:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD, config.RC_RATE_LIMITER_SETTING, True)
            except Exception:  # noqa: BLE001
                warn(emit, "could not restore the API rate limiter setting", phase="restore")
        if metrics_changed:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD, monitoring.RC_METRICS_SETTING, False)
            except Exception:  # noqa: BLE001
                warn(emit, "could not restore the Prometheus metrics setting", phase="restore")
        for problem in constrain_mod.restore(applied):
            warn(emit, f"could not restore resource limits - {problem}", phase="restore")
        (runner.workspace(m.name) / "loadtest" / "users.json").unlink(missing_ok=True)

    if last_pass is None:
        result = f"breaches the SLO even at {req.start} VUs - start lower"
    elif first_fail is None:
        result = f"holds the SLO up to {last_pass} VUs (never breached; raise max to push further)"
    else:
        result = f"~{last_pass} concurrent VUs (holds at {last_pass}, breaks at {first_fail})"
    breach = (next((r for r in steps if r["vus"] == first_fail), None) if first_fail
              else next((r for r in steps if not r["ok"]), None))
    why = ""
    if breach:
        why = (f"at {breach['vus']} VUs the RC event loop saturated (lag peaked "
               f"at {breach['lag_max_s'] * 1000:.0f}ms)" if breach["lag_max_s"] >= 0.5
               else f"at {breach['vus']} VUs: {'; '.join(breach['breached'])}")
    info(emit, f"capacity: {result}", phase="done", data={"capacity_vus": last_pass})
    return {"ctx": {"name": m.name, "scenario": req.scenario, "slo": req.slo,
                    "constrained": constrain_mod.human(per_service) if per_service else ""},
            "steps": steps, "capacity_vus": last_pass, "breach_vus": first_fail,
            "result": result, "why": why}


# --- benchmark ---------------------------------------------------------------

def bench_metrics(resolved, boot_s: float, seed_total_s: float, s: dict, res: dict, name: str) -> dict:
    lat, d = s.get("latency", {}), s.get("durations", {})
    resources = {_short(full, name): {
        "idle_cpu": st.idle_cpu, "peak_cpu": st.peak_cpu, "idle_mem": st.idle_mem,
        "peak_mem": st.peak_mem, "limit_mem": st.limit_mem} for full, st in res.items()}

    def peak(short, key):
        return resources.get(short, {}).get(key, 0.0)

    msg_dur, user_dur = d.get("messages", 0.0), d.get("users", 0.0)
    return {"mongo": f"{resolved.mongo_tag} ({resolved.mongo_flavor})",
            "image": f"{resolved.rc_image}:{resolved.rc_version}",
            "boot_s": boot_s, "seed_total_s": seed_total_s,
            "users": s["users"], "user_rate": s["users"] / user_dur if user_dur > 0.05 else 0.0,
            "messages": s["messages"], "msg_rate": s["messages"] / msg_dur if msg_dur > 0.05 else 0.0,
            "msg_p95_ms": lat.get("p95", 0.0), "msg_p99_ms": lat.get("p99", 0.0),
            "rc_cpu": peak("rocketchat", "peak_cpu"), "mongo_cpu": peak("mongodb", "peak_cpu"),
            "rc_mem_mb": peak("rocketchat", "peak_mem") / 1e6,
            "seed": s, "resources": resources}


def bench_one(version: str, profile: str, offline: bool, no_pull: bool, emit: Emit = null_emit) -> dict:
    """Boot one version, run the seed workload under resource monitoring, tear it
    down. Returns a metrics dict (ok=False + error on any failure; never raises)."""
    from rc_repro import compose, perf, versions
    result = {"version": version, "ok": False, "error": ""}
    try:
        resolved = versions.resolve(version, offline=offline)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    name = "bench-" + lifecycle.sanitize(version)
    if runner.exists(name):
        existing = runner.read_meta(name)
        if not (isinstance(existing.extra, dict) and existing.extra.get("benchmark")):
            result["error"] = f"a non-benchmark repro named {name!r} exists - remove it first"
            return result
        runner.down(name, volumes=True)
        runner.remove(name)
    mon = None
    try:
        pre = presets_load_default()
        host_port = runner.pick_port()
        spec = compose.Spec.from_resolved(
            resolved, project_name=runner.project_name(name),
            root_url=f"http://localhost:{host_port}", host_port=host_port, reg_token=None, preset=pre)
        meta = runner.Metadata(
            name=name, project=spec.project_name, rc_version=resolved.rc_version,
            rc_image=resolved.rc_image, mongo_tag=resolved.mongo_tag, mongo_flavor=resolved.mongo_flavor,
            preset="default", root_url=spec.root_url, host_port=host_port,
            version_source=resolved.source, extra={"benchmark": True})
        runner.write(name, compose.to_yaml(compose.build(spec)), meta)
        info(emit, f"[{version}] booting on {meta.root_url}", phase="boot")
        if runner.up(name, pull=not no_pull) != 0:
            result["error"] = "docker compose up failed"
            return result
        t0 = time.monotonic()
        rcapi.wait_ready(meta.root_url, timeout=300.0,
                         is_alive=lambda: runner.rc_state(name) in ("running", "restarting", "created"))
        boot_s = time.monotonic() - t0
        auth = lifecycle.finalize(meta, null_emit) or rcapi.login(meta.root_url)
        plan = seeder.plan_from(profile)
        info(emit, f"[{version}] seeding ({profile})", phase="seed")
        mon = perf.ResourceMonitor(name).start()
        ts = time.monotonic()
        s = seeder.seed(meta.root_url, auth, plan, log=lambda _m: None)
        seed_total = time.monotonic() - ts
        res = mon.stop()
        mon = None
        result.update(bench_metrics(resolved, boot_s, seed_total, s, res, name))
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        if mon:
            mon.stop()
        try:
            runner.down(name, volumes=True)
            runner.remove(name)
        except Exception:  # noqa: BLE001
            pass
    return result


def presets_load_default():
    from rc_repro import presets
    return presets.load("default")


def run_benchmark(versions_list: list[str], seed_profile: str = "standard",
                  offline: bool = False, no_pull: bool = False, regress_pct: float = 25.0,
                  emit: Emit = null_emit) -> dict:
    from rc_repro.perf import report as perf_report
    lifecycle.require_docker()
    vers = [v.strip() for v in versions_list if v.strip()]
    if len(vers) < 2:
        raise ValidationError("give at least two versions to compare")
    results = []
    prev = None
    for v in vers:
        info(emit, f"benchmarking {v} ({len(results) + 1}/{len(vers)})", phase="boot")
        r = bench_one(v, seed_profile, offline, no_pull, emit)
        r["flag"] = perf_report.regression_flag(r, prev, regress_pct)
        if r.get("ok"):
            prev = r
        results.append(r)
    ok = sum(1 for r in results if r.get("ok"))
    info(emit, f"benchmark done: {ok}/{len(vers)} succeeded", phase="done")
    return {"results": results, "profile": seed_profile, "regress_pct": regress_pct}

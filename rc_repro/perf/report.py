"""Render benchmark results — regression flags, a console summary table, and a
detailed shareable markdown report."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rc_repro import config
from rc_repro.perf.timings import fmt_ms


def regression_flag(cur: dict, prev: dict | None, pct: float) -> str:
    """Flag `cur` vs the previous version: seed time or p95 up more than `pct`%."""
    if not prev or not cur.get("ok") or not prev.get("ok"):
        return ""
    flags = []
    if prev.get("seed_total_s") and cur["seed_total_s"] / prev["seed_total_s"] - 1 > pct / 100:
        flags.append(f"seed +{(cur['seed_total_s'] / prev['seed_total_s'] - 1) * 100:.0f}%")
    if prev.get("msg_p95_ms") and cur["msg_p95_ms"] / prev["msg_p95_ms"] - 1 > pct / 100:
        flags.append(f"p95 +{(cur['msg_p95_ms'] / prev['msg_p95_ms'] - 1) * 100:.0f}%")
    return "regression: " + ", ".join(flags) if flags else ""


def _rate(count, secs) -> str:
    return f"{count / secs:.1f}/s" if secs and secs > 0.05 and count else "-"


# ---------------------------------------------------------------- console table

_HEADERS = ["VERSION", "MONGO", "BOOT", "SEED", "msg/s", "p95", "RC CPU", "MongoCPU", "RC RAM"]


def _cells(r: dict) -> list[str]:
    if not r.get("ok"):
        return [r["version"], "FAILED", r.get("error", "")[:40], "", "", "", "", "", ""]
    return [
        r["version"], r["mongo"], f"{r['boot_s']:.1f}s", f"{r['seed_total_s']:.1f}s",
        f"{r['msg_rate']:.1f}", f"{r['msg_p95_ms']:.0f}ms", f"{r['rc_cpu']:.0f}%",
        f"{r['mongo_cpu']:.0f}%", f"{r['rc_mem_mb']:.0f}MB",
    ]


def table_rows(results: list[dict], pct: float) -> tuple[list[str], list[str], list[str]]:
    """(header_line, row_lines, flags) for the console, columns padded to width."""
    n = len(_HEADERS)
    body = [(_cells(r) + [""] * n)[:n] for r in results]   # pad short (FAILED) rows
    flags, prev = [], None
    for r in results:
        flags.append(regression_flag(r, prev, pct))
        if r.get("ok"):
            prev = r
    widths = [max(len(_HEADERS[i]), *(len(row[i]) for row in body)) for i in range(n)]
    fmt = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths))
    return [fmt(_HEADERS)], [fmt(row) for row in body], flags


# ------------------------------------------------------------- markdown report

def _summary_table(results: list[dict], pct: float) -> list[str]:
    cols = ["VERSION", "MONGO", "BOOT", "SEED", "users/s", "msg/s", "msg p95",
            "RC CPU", "Mongo CPU", "RC RAM", "notes"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    prev = None
    for r in results:
        if r.get("ok"):
            cells = [r["version"], r["mongo"], f"{r['boot_s']:.1f}s", f"{r['seed_total_s']:.1f}s",
                     f"{r['user_rate']:.1f}", f"{r['msg_rate']:.1f}", f"{r['msg_p95_ms']:.0f}ms",
                     f"{r['rc_cpu']:.0f}%", f"{r['mongo_cpu']:.0f}%", f"{r['rc_mem_mb']:.0f}MB"]
        else:
            cells = [r["version"], "FAILED", r.get("error", "")[:60]] + [""] * 7
        lines.append("| " + " | ".join(cells) + f" | {regression_flag(r, prev, pct)} |")
        if r.get("ok"):
            prev = r
    return lines


def _version_detail(r: dict) -> list[str]:
    if not r.get("ok"):
        return [f"### {r['version']} — FAILED", "", f"> {r.get('error', '')}", ""]
    s, d, lat = r["seed"], r["seed"].get("durations", {}), r["seed"].get("latency", {})
    out = [
        f"### {r['version']}", "",
        f"- Image: `{r['image']}`  ·  Mongo: {r['mongo']}",
        f"- Boot to ready: **{r['boot_s']:.1f}s**  ·  Seed total: **{r['seed_total_s']:.1f}s**",
        "",
        "**Workload created & phase timing**", "",
        "| phase | count | time | rate |", "|---|---|---|---|",
        f"| users | {s['users']} | {d.get('users', 0):.1f}s | {_rate(s['users'], d.get('users', 0))} |",
        f"| channels | {s['channels']} | {d.get('channels', 0):.1f}s | {_rate(s['channels'], d.get('channels', 0))} |",
        f"| messages | ~{s['messages']} | {d.get('messages', 0):.1f}s | {_rate(s['messages'], d.get('messages', 0))} |",
        f"| DMs | {s['dms']} | {d.get('dms', 0):.1f}s | {_rate(s['dms'], d.get('dms', 0))} |",
        "",
    ]
    if lat.get("count"):
        out += [
            "**Message latency** (`chat.postMessage`)", "",
            f"- {lat['count']} calls · mean {fmt_ms(lat['mean'])} · min {fmt_ms(lat['min'])} · max {fmt_ms(lat['max'])}",
            f"- p50 {fmt_ms(lat['p50'])} · p90 {fmt_ms(lat['p90'])} · p95 {fmt_ms(lat['p95'])} · p99 {fmt_ms(lat['p99'])}",
            "",
        ]
    out += ["**Resource peaks during seed**", "",
            "| container | idle CPU | peak CPU | peak RAM | mem limit |", "|---|---|---|---|---|"]
    for cname, rr in sorted(r.get("resources", {}).items()):
        out.append(
            f"| {cname} | {rr['idle_cpu']:.0f}% | {rr['peak_cpu']:.0f}% | "
            f"{rr['peak_mem'] / 1e6:.0f}MB | {rr['limit_mem'] / 1e6:.0f}MB |"
        )
    out.append("")
    return out


def benchmark_markdown(results: list[dict], profile: str, pct: float, host: dict) -> str:
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# rc-repro benchmark report", "",
        f"- **Generated:** {when}",
        f"- **Host:** {host.get('os', '?')} · {host.get('cpu', '?')} CPUs · "
        f"Docker {host.get('docker', '?')} · Compose {host.get('compose', '?')}",
        f"- **Workload:** seed profile `{profile}` — run identically against every version",
        f"- **Regression threshold:** +{pct:.0f}% (seed time or p95) vs the previous version",
        "",
        "> Absolute numbers are host-specific (on Docker Desktop, CPU/RAM reflect the "
        "Docker VM, not the laptop). The **deltas between versions** are the signal; "
        "versions are booted **sequentially on the same host** in fresh, isolated "
        "environments (each torn down before the next).",
        "",
        "## Summary", "",
    ]
    lines += _summary_table(results, pct)
    lines += [
        "", "## What the workload did", "",
        f"Each version ran the **identical** seed (profile `{profile}`):",
        "",
        "- **Users** — create N verified users (`alice`, `bob`, …) and log in as each, "
        "so messages are authored by different users, not just the admin.",
        "- **Channels** — create public channels, each with a random subset of users as members.",
        "- **Messages** — post messages via `chat.postMessage` across the channels, a couple "
        "of private groups, and `general`; every call's latency is recorded.",
        "- **DMs** — open direct-message pairs between random users and post a message in each.",
        "",
        "_During seeding, email-2FA and the API rate limiter are temporarily disabled so it "
        "can log in as each user and post at volume, then both are restored._",
        "",
        "## Per-version detail", "",
    ]
    for r in results:
        lines += _version_detail(r)
    return "\n".join(lines) + "\n"


def write_benchmark(results: list[dict], profile: str, pct: float, stamp: str,
                    host: dict, dest: str | None = None) -> str:
    """Write the report. `dest` (—report-path) may be a file or a directory;
    default is <RC_REPRO_HOME>/reports/benchmark-<stamp>.md."""
    filename = f"benchmark-{stamp}.md"
    return _write(benchmark_markdown(results, profile, pct, host), filename, dest)


# --------------------------------------------------------------- loadtest report

_SCENARIO_DESC = {
    "messages": "POST `chat.postMessage` to `#general` — the write path.",
    "login": "POST `/api/v1/login` repeatedly — auth throughput.",
    "read": "GET `channels.history` — the read path.",
    "mixed": "60% reads, 30% posts, 10% logins — a realistic blend.",
    "journey": "a full user session per iteration — login → rooms → open → post → sync, each step timed.",
    "custom": "a caller-supplied endpoint.",
}


def _status_breakdown(summary: dict) -> str:
    """'2xx 1158 · 429 61 · 5xx 41' from the summary's status buckets (non-zero only)."""
    st = summary.get("status") or {}
    order = ["2xx", "429", "4xx", "5xx", "other"]
    parts = [f"{k} {int(st[k])}" for k in order if st.get(k)]
    return " · ".join(parts)


def loadtest_markdown(ctx: dict, summary: dict, slo_results: list[dict],
                      resources: dict | None, host: dict,
                      snapshot: dict | None = None, compare: dict | None = None,
                      diag: dict | None = None) -> str:
    """Render a shareable load-test report. `ctx` carries the run parameters
    (repro, version, scenario, vus, duration, ramp, target, users); `snapshot`
    the workspace context; `compare` an optional baseline diff
    ({label, saved_at, rows})."""
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    load = (f"ramp {ctx['ramp']} VUs" if ctx.get("ramp") else f"{ctx['vus']} VUs") + \
        f" for {ctx['duration']}"
    identity = (f"{ctx['users']} seeded users (round-robin per VU)"
                if ctx.get("users") else "the admin token")
    lines = [
        "# rc-repro load-test report", "",
        f"- **Generated:** {when}",
        f"- **Repro:** `{ctx['name']}` — Rocket.Chat {ctx['version']}",
        f"- **Host:** {host.get('os', '?')} · {host.get('cpu', '?')} CPUs · "
        f"Docker {host.get('docker', '?')} · Compose {host.get('compose', '?')}",
        f"- **Scenario:** `{ctx.get('label', ctx['scenario'])}` — "
        f"{_SCENARIO_DESC.get(ctx['scenario'], '')}",
        f"- **Load:** {load}, as **{identity}**, generated by k6 on the repro's "
        f"docker network (target `{ctx['target']}`)",
        "",
        "> k6 drives load against the **internal** service address (no host-port "
        "round trip). The REST rate limiter is disabled for the run and restored "
        "after. Absolute numbers are host-specific; use them to compare runs on "
        "the same machine.",
        "",
    ]
    if snapshot:
        lines += ["## Workspace", "", "| | |", "|---|---|",
                  f"| Rocket.Chat | {snapshot.get('rc_version', '?')} |",
                  f"| preset | {snapshot.get('preset', '?')} |",
                  f"| RC instances | {snapshot.get('instances', 1)} |"]
        for k, lbl in (("users", "users"), ("rooms", "rooms"), ("messages", "messages")):
            if snapshot.get(k) is not None:
                lines.append(f"| {lbl} in workspace | {snapshot[k]} |")
        if snapshot.get("constraints"):
            lines.append(f"| **resource caps** | {snapshot['constraints']} (via docker update, "
                         "restored after the test) |")
        lines.append("")
    lines += [
        "## Results", "",
        "| metric | value |", "|---|---|",
        f"| requests | {summary.get('count', 0):.0f} |",
        f"| throughput | **{summary.get('rps', 0):.1f} req/s** |",
        f"| latency p50 | {summary.get('p50', 0):.0f}ms |",
        f"| latency p90 | {summary.get('p90', 0):.0f}ms |",
        f"| latency p95 | **{summary.get('p95', 0):.0f}ms** |",
        f"| latency p99 | {summary.get('p99', 0):.0f}ms |",
        f"| latency avg / min / max | {summary.get('avg', 0):.0f} / "
        f"{summary.get('min', 0):.0f} / {summary.get('max', 0):.0f} ms |",
        f"| error rate | {summary.get('error_rate', 0) * 100:.2f}% |",
        f"| checks passed | {summary.get('checks_rate', 0) * 100:.1f}% |",
    ]
    responses = _status_breakdown(summary)
    if responses:
        lines.append(f"| responses | {responses} |")
    lines.append("")
    if diag and diag.get("verdict"):
        lines += ["## Verdict", ""]
        lines += [f"- {v}" for v in diag["verdict"]]
        lines.append("")
    steps = summary.get("steps") or {}
    if steps:
        from rc_repro.perf.baseline import step_order
        lines += ["## Per-step latency", "",
                  "| step | count | p50 | p95 | p99 |", "|---|---|---|---|---|"]
        for s in step_order(steps):
            v = steps[s]
            lines.append(f"| {s} | {v.get('count', 0):.0f} | {fmt_ms(v.get('p50') or 0)} | "
                         f"{fmt_ms(v.get('p95') or 0)} | {fmt_ms(v.get('p99') or 0)} |")
        lines.append("")
    if diag and diag.get("timeline"):
        tl = diag["timeline"]
        lines += [f"## Latency over time ({tl['width_s']}s buckets)", "",
                  "| t | reqs | p50 | p95 | max | errors |", "|---|---|---|---|---|---|"]
        for b in tl["buckets"]:
            lines.append(f"| {b['t0']}s | {b['reqs']} | {fmt_ms(b['p50'])} | "
                         f"{fmt_ms(b['p95'])} | {fmt_ms(b['max'])} | {b['errors'] or ''} |")
        if tl.get("first_error_s") is not None:
            lines.append("")
            lines.append(f"> Errors began ~{tl['first_error_s']}s into the run.")
        lines.append("")
    if diag and diag.get("spike"):
        sp = diag["spike"]
        rec = sp.get("recovered_after_s")
        lines += ["## Spike recovery", "",
                  f"- baseline p95 {fmt_ms(sp['baseline_p95'])} → spike p95 "
                  f"{fmt_ms(sp['spike_p95'])} → "
                  + (f"**recovered ~{rec}s** after load dropped" if rec is not None
                     else "**did not recover** within the run"),
                  ""]
    if diag and diag.get("rcmetrics"):
        lines += ["## RC internals during the test", "",
                  "| instance | event-loop lag peak | lag p99 | heap peak | ddp users |",
                  "|---|---|---|---|---|"]
        for svc in sorted(diag["rcmetrics"]):
            m = diag["rcmetrics"][svc]
            peak = m.get("eventloop_lag_max_s") or m.get("eventloop_lag_s")
            p99 = m.get("eventloop_lag_p99_s")
            heap, ddp = m.get("heap_used_bytes"), m.get("ddp_users")
            peak_s = fmt_ms(peak["max"] * 1000) if peak else "-"
            p99_s = fmt_ms(p99["max"] * 1000) if p99 else "-"
            heap_s = f"{heap['max'] / 1e6:.0f}MB" if heap else "-"
            ddp_s = f"{ddp['max']:.0f}" if ddp else "-"
            lines.append(f"| {svc} | {peak_s} | {p99_s} | {heap_s} | {ddp_s} |")
        lines += ["", "> Event-loop lag is the Node saturation signal: once the loop lags, "
                  "every request queues behind it.", ""]
    if diag and diag.get("mongo") and (diag["mongo"].get("slow") or diag["mongo"].get("total")):
        mg = diag["mongo"]
        lines += [f"## Slow MongoDB queries ({mg.get('total', 0)} profiled, "
                  f"{mg.get('collscan', 0)} COLLSCAN)", ""]
        if mg.get("slow"):
            lines += ["| time | namespace | op | plan | docs examined | returned |",
                      "|---|---|---|---|---|---|"]
            for s in mg["slow"]:
                lines.append(f"| {fmt_ms(s['millis'])} | {s['ns']} | {s['op']} | "
                             f"{s.get('plan') or '?'} | {s['docs']} | {s['ret']} |")
        lines.append("")
    if compare and compare.get("rows"):
        lines += [f"## vs baseline `{compare.get('label', '?')}` "
                  f"(saved {str(compare.get('saved_at', ''))[:19]})", "",
                  "| metric | baseline | this run | delta | |", "|---|---|---|---|---|"]
        for r in compare["rows"]:
            fmt = (lambda v: f"{v:.1f}") if "rps" in r["metric"] else \
                  (lambda v: f"{v * 100:.2f}%") if "error" in r["metric"] else \
                  (lambda v: fmt_ms(v))
            flag = "**regression**" if r["flag"] else ("improved" if not r["worse"] and abs(r["pct"]) > 25 else "")
            lines.append(f"| {r['metric']} | {fmt(r['before'])} | {fmt(r['after'])} | "
                         f"{r['pct']:+.0f}% | {flag} |")
        lines.append("")
    if slo_results:
        passed = all(r["ok"] for r in slo_results)
        lines += [f"## SLO gate — {'PASS' if passed else 'FAIL'}", "",
                  "| rule | threshold | actual | result |", "|---|---|---|---|"]
        for r in slo_results:
            from rc_repro.perf.slo import fmt_actual
            mark = "PASS" if r["ok"] else "FAIL"
            actual = ("not measured" if not r.get("measured", True)
                      else fmt_actual(r["key"], r["actual"]))
            lines.append(f"| {r['key']} | {r['op']} {r['raw']} | {actual} | {mark} |")
        lines.append("")
    if resources:
        lines += ["## Resource cost during the test", "",
                  "| container | idle CPU | peak CPU | peak RAM | mem limit |",
                  "|---|---|---|---|---|"]
        for cname in sorted(resources):
            rr = resources[cname]
            lines.append(
                f"| {cname} | {rr.idle_cpu:.0f}% | {rr.peak_cpu:.0f}% | "
                f"{rr.peak_mem / 1e6:.0f}MB | {rr.limit_mem / 1e6:.0f}MB |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_loadtest(ctx: dict, summary: dict, slo_results: list[dict],
                   resources: dict | None, host: dict, stamp: str,
                   dest: str | None = None, snapshot: dict | None = None,
                   compare: dict | None = None, diag: dict | None = None) -> str:
    filename = f"loadtest-{ctx['scenario']}-{stamp}.md"
    md = loadtest_markdown(ctx, summary, slo_results, resources, host,
                           snapshot=snapshot, compare=compare, diag=diag)
    return _write(md, filename, dest)


def write_capacity(ctx: dict, steps: list[dict], result: str, why: str,
                   host: dict, stamp: str, dest: str | None = None) -> str:
    """Shareable markdown for a capacity search."""
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# rc-repro capacity report", "",
        f"- **Generated:** {when}",
        f"- **Repro:** `{ctx['name']}` — Rocket.Chat {ctx['version']}",
        f"- **Host:** {host.get('os', '?')} · {host.get('cpu', '?')} CPUs · "
        f"Docker {host.get('docker', '?')} · Compose {host.get('compose', '?')}",
        f"- **Workload:** `{ctx['scenario']}` as "
        f"{ctx['users'] or 'the admin token — no'} seeded users, "
        f"steps of {ctx['step_duration']} (target `{ctx['target']}`)",
        f"- **SLO:** `{ctx['slo']}`",
    ]
    if ctx.get("constrained"):
        lines.append(f"- **Resource caps:** {ctx['constrained']} (docker update, restored after)")
    lines += [
        "", f"## Result — {result}", "",
    ]
    if why:
        lines += [f"> {why}", ""]
    lines += ["| VUs | req/s | p95 | error rate | event-loop lag peak | SLO |",
              "|---|---|---|---|---|---|"]
    for s in sorted(steps, key=lambda r: r["vus"]):
        lines.append(
            f"| {s['vus']} | {s['rps']:.1f} | {fmt_ms(s['p95'])} | "
            f"{s['error_rate'] * 100:.2f}% | {fmt_ms(s['lag_max_s'] * 1000)} | "
            + ("PASS" if s["ok"] else "**FAIL** — " + "; ".join(s["breached"])) + " |")
    lines += ["", "> Steps double until the SLO breaks, then bisect between the last "
              "pass and first fail. Absolute numbers are host-specific; the shape "
              "(where it breaks, and why) is the signal.", ""]
    return _write("\n".join(lines) + "\n", f"capacity-{stamp}.md", dest)


def _write(content: str, filename: str, dest: str | None) -> str:
    """Write `content`; `dest` may be a file or directory, default reports_dir()."""
    if dest:
        path = Path(dest).expanduser()
        if path.is_dir() or str(dest).endswith(("/", "\\")):
            path = path / filename
    else:
        path = config.reports_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)

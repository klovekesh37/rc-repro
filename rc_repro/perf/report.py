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
    "custom": "a caller-supplied endpoint.",
}


def _status_breakdown(summary: dict) -> str:
    """'2xx 1158 · 429 61 · 5xx 41' from the summary's status buckets (non-zero only)."""
    st = summary.get("status") or {}
    order = ["2xx", "429", "4xx", "5xx", "other"]
    parts = [f"{k} {int(st[k])}" for k in order if st.get(k)]
    return " · ".join(parts)


def loadtest_markdown(ctx: dict, summary: dict, slo_results: list[dict],
                      resources: dict | None, host: dict) -> str:
    """Render a shareable load-test report. `ctx` carries the run parameters
    (repro, version, scenario, vus, duration, ramp, target)."""
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    load = (f"ramp {ctx['ramp']} VUs" if ctx.get("ramp") else f"{ctx['vus']} VUs") + \
        f" for {ctx['duration']}"
    lines = [
        "# rc-repro load-test report", "",
        f"- **Generated:** {when}",
        f"- **Repro:** `{ctx['name']}` — Rocket.Chat {ctx['version']}",
        f"- **Host:** {host.get('os', '?')} · {host.get('cpu', '?')} CPUs · "
        f"Docker {host.get('docker', '?')} · Compose {host.get('compose', '?')}",
        f"- **Scenario:** `{ctx.get('label', ctx['scenario'])}` — "
        f"{_SCENARIO_DESC.get(ctx['scenario'], '')}",
        f"- **Load:** {load}, generated by k6 on the repro's docker network "
        f"(target `{ctx['target']}`)",
        "",
        "> k6 drives load against the **internal** service address (no host-port "
        "round trip). The REST rate limiter is disabled for the run and restored "
        "after. Absolute numbers are host-specific; use them to compare runs on "
        "the same machine.",
        "",
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
                   dest: str | None = None) -> str:
    filename = f"loadtest-{ctx['scenario']}-{stamp}.md"
    md = loadtest_markdown(ctx, summary, slo_results, resources, host)
    return _write(md, filename, dest)


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

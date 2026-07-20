"""Rule-based diagnosis of a load-test run.

Combines the client-side result (latency/errors) with the server-side signals
Phase C collects — RC's event-loop lag, Mongo's slow-query profile, container
CPU — into a few plain-language findings: not "p95 was 900ms" but "RC's event
loop saturated; Mongo was idle — the Node process is the bottleneck".

Deliberately hedged wording ("likely") and simple thresholds; this is triage
evidence for a ticket, not an oracle.
"""

from __future__ import annotations

from rc_repro.perf.timings import fmt_ms

# Node event-loop lag: >100ms mean is degraded, >500ms peak is saturated.
LAG_MEAN_WARN_S = 0.1
LAG_MAX_SAT_S = 0.5


def analyze(summary: dict, *, rcmetrics: dict | None = None, mongo: dict | None = None,
            resources: dict | None = None, timeline: dict | None = None,
            soak: dict | None = None, spike: dict | None = None) -> list[str]:
    """Return 1-5 plain-language findings, most important first. `soak` is
    {container: bytes_per_hour} RAM slopes from a long run; `spike` is
    timeline.spike_recovery()'s result for a --spike run."""
    findings: list[str] = []

    # 0. Spike recovery — only worth a finding when it did NOT bounce back.
    if spike:
        if spike.get("recovered_after_s") is None:
            findings.append(
                f"Did not recover from the spike within the run: p95 stayed above "
                f"1.5x the pre-spike baseline ({fmt_ms(spike['baseline_p95'])}) after "
                "load dropped — connections/queues are not draining.")
        elif spike["recovered_after_s"] > 30:
            findings.append(
                f"Slow spike recovery: p95 took ~{spike['recovered_after_s']}s to return "
                f"to normal after load dropped (baseline {fmt_ms(spike['baseline_p95'])}).")

    # 1. RC event loop — THE Node saturation signal. Judge by the per-interval
    # lag histogram peak (eventloop_lag_max_s); the instantaneous gauge samples
    # BETWEEN stalls and stays near zero even while requests queue.
    worst_svc, worst = None, None
    for svc, m in (rcmetrics or {}).items():
        lag = m.get("eventloop_lag_max_s") or m.get("eventloop_lag_s")
        if lag and (worst is None or lag["max"] > worst["max"]):
            worst_svc, worst = svc, lag
    lag_found = False
    if worst:
        if worst["max"] >= LAG_MAX_SAT_S:
            lag_found = True
            findings.append(
                f"RC event loop saturated: lag peaked at {fmt_ms(worst['max'] * 1000)} "
                f"on {worst_svc} — the Node process is the bottleneck; more CPU or more "
                "instances (multi-instance preset) will help.")
        elif worst["mean"] >= LAG_MEAN_WARN_S:
            # Degraded is judged on the SUSTAINED lag (mean), not a lone peak —
            # a single ~100ms peak is routine and shouldn't read as "degraded".
            lag_found = True
            findings.append(
                f"RC event loop degraded: lag averaged {fmt_ms(worst['mean'] * 1000)} "
                f"(peaks to {fmt_ms(worst['max'] * 1000)}) on {worst_svc} — "
                "approaching CPU saturation at this load.")

    # 2. Mongo slow queries / missing indexes.
    if mongo:
        slow = mongo.get("slow") or []
        if mongo.get("collscan"):
            # Name the slowest COLLSCAN only if one made the top list — citing a
            # non-COLLSCAN row here would point at the wrong query.
            top = next((s for s in slow if "COLLSCAN" in (s.get("plan") or "")), None)
            where = f" — slowest: {top['ns']} {fmt_ms(top['millis'])}" if top else ""
            findings.append(
                f"MongoDB ran {mongo['collscan']} collection scan(s) (COLLSCAN) among the "
                f"profiled slow queries — likely a missing index{where}.")
        elif slow:
            top = slow[0]
            findings.append(
                f"{mongo.get('total', len(slow))} MongoDB quer(ies) exceeded the slow "
                f"threshold — slowest: {top['ns']} {top['op']} {fmt_ms(top['millis'])} "
                f"({top['plan'] or 'no plan'}).")

    # 3. Where the CPU actually went (needs --stats).
    if resources:
        peaks = {svc: getattr(st, "peak_cpu", 0.0) for svc, st in resources.items()}
        rc_peak = max((v for k, v in peaks.items() if k.startswith("rocketchat")), default=0.0)
        mongo_peak = peaks.get("mongodb", 0.0)
        if mongo_peak > 85 and mongo_peak > rc_peak * 1.5:
            findings.append(
                f"MongoDB was the hot container ({mongo_peak:.0f}% CPU vs RC {rc_peak:.0f}%) "
                "— likely Mongo-bound at this load.")
        elif rc_peak > 85 and rc_peak > mongo_peak * 1.5 and not lag_found:
            findings.append(
                f"Rocket.Chat was the hot container ({rc_peak:.0f}% CPU vs Mongo "
                f"{mongo_peak:.0f}%) — likely CPU-bound in RC.")

    # 4. Errors, with the status classes and when they started.
    err = summary.get("error_rate", 0.0)
    if err > 0.01:
        st = summary.get("status") or {}
        kinds = []
        if st.get("429"):
            kinds.append(f"{int(st['429'])}x 429 (throttled)")
        if st.get("5xx"):
            kinds.append(f"{int(st['5xx'])}x 5xx (server errors)")
        if st.get("4xx"):
            kinds.append(f"{int(st['4xx'])}x 4xx")
        when = ""
        if timeline and timeline.get("first_error_s") is not None:
            when = f", starting ~{timeline['first_error_s']}s into the run"
        findings.append(f"{err * 100:.1f}% of requests failed"
                        + (f": {', '.join(kinds)}" if kinds else "") + when + ".")

    # 4b. Sustained-run RAM growth (soak signal) — only reported for long runs.
    for svc, per_hour in (soak or {}).items():
        if per_hour > 30e6:   # > ~30MB/h upward is worth an eyebrow
            findings.append(
                f"{svc} RAM grew ~{per_hour / 1e6:.0f}MB/h over this sustained run — "
                "if a longer soak repeats this slope, treat it as a leak candidate.")

    # 5. High client latency with NO server-side signal: time is being spent
    # queueing outside the event loop (CPU throttling/CFS, Mongo waits,
    # connection backlog) — say so instead of a false "healthy".
    if not findings and summary.get("p95", 0) >= 1000:
        findings.append(
            f"High client latency (p95 {summary.get('p95', 0):.0f}ms) without RC "
            "event-loop saturation — requests are queueing outside the Node loop "
            "(CPU throttling, Mongo waits, or connection backlog). If this run was "
            "--constrain'ed, that's the cap at work.")

    if not findings:
        findings.append(
            f"No saturation signals: p95 {summary.get('p95', 0):.0f}ms at "
            f"{summary.get('rps', 0):.1f} req/s, {summary.get('error_rate', 0) * 100:.2f}% "
            "errors — the workspace has headroom at this load.")
    return findings

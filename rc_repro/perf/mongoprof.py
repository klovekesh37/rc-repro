"""Capture MongoDB slow queries during a load test.

Turns on Mongo's query profiler (level 1: log ops slower than `slowms`) for the
duration of the run, then reads `system.profile` back: the slowest queries, and
whether they COLLSCAN — "slow because there's no index on X" is the single most
useful line of evidence a perf ticket can carry.

Everything runs through `docker compose exec` on the repro's mongodb container
(mongosh, falling back to the legacy `mongo` shell for old pairings), is
version-tolerant, and is strictly best-effort: any failure degrades to
"slow-query capture unavailable" without affecting the test.
"""

from __future__ import annotations

import json

from rc_repro import runner

DB = "rocketchat"
_URI = f"mongodb://localhost:27017/{DB}"


def _eval(name: str, js: str) -> str | None:
    """Run JS in the repro's mongodb container; mongosh first, legacy mongo as
    fallback. Returns stdout, or None if neither shell worked."""
    for shell in ("mongosh", "mongo"):
        rc, out = runner.compose_exec_capture(
            name, "mongodb", [shell, "--quiet", _URI, "--eval", js])
        if rc == 0 and out.strip():
            return out
    return None


def _last_json(out: str):
    """The last stdout line that parses as JSON (shells may print banners first)."""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def start(name: str, slowms: int = 100) -> dict | None:
    """Enable profiling (level 1, > slowms) on the rocketchat DB. Returns the
    prior {was, slowms} for `stop`, or None if profiling isn't available."""
    js = (
        "var prev = db.getProfilingStatus();"
        f"db.setProfilingLevel(1, {int(slowms)});"   # numeric arg: works in old shells too
        'print(JSON.stringify({was: prev.was, slowms: prev.slowms}));'
    )
    out = _eval(name, js)
    prior = _last_json(out) if out else None
    return prior if isinstance(prior, dict) and "was" in prior else None


def stop(name: str, prior: dict | None) -> None:
    """Restore the prior profiling level (best-effort)."""
    if not prior:
        return
    js = f"db.setProfilingLevel({int(prior.get('was', 0))}, {int(prior.get('slowms', 100))});"
    _eval(name, js)


def collect(name: str, since_epoch_ms: int, limit: int = 5) -> dict | None:
    """Slow ops profiled since `since_epoch_ms`:
    {total, collscan, slow: [{ns, op, millis, plan, docs, keys, ret, cmd}]}."""
    js = (
        f'var q = {{ts: {{$gte: new Date({int(since_epoch_ms)})}},'
        f' ns: {{$ne: "{DB}.system.profile"}}}};'
        "var docs = db.system.profile.find(q).sort({millis: -1}).limit(200).toArray();"
        "var out = {total: docs.length, collscan: 0, slow: []};"
        "for (var i = 0; i < docs.length; i++) {"
        "  var d = docs[i];"
        "  var plan = d.planSummary || '';"
        "  if (plan.indexOf('COLLSCAN') >= 0) out.collscan++;"
        f" if (out.slow.length < {int(limit)}) {{"
        "    var cmd = '';"
        "    try { cmd = JSON.stringify(d.command || d.query || {}).slice(0, 160); } catch (e) {}"
        "    out.slow.push({ns: d.ns || '', op: d.op || '', millis: d.millis || 0,"
        "      plan: plan, docs: d.docsExamined || 0, keys: d.keysExamined || 0,"
        "      ret: d.nreturned || 0, cmd: cmd});"
        "  }"
        "}"
        "print(JSON.stringify(out));"
    )
    out = _eval(name, js)
    parsed = _last_json(out) if out else None
    return parsed if isinstance(parsed, dict) and "slow" in parsed else None

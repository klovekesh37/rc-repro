"""Durable notes about side effects that must be undone, so a killed process does
not leave them behind.

WHY A FILE AND NOT A `finally`. The GUI runs long operations on daemon threads, so
`systemctl restart`, an OOM kill or a plain SIGKILL ends them where they stand and
skips every `finally` they were relying on. Those blocks are doing real work:
`backup` stops Rocket.Chat for the duration of the dump, `loadtest` turns the API
rate limiter OFF, `capacity` arms the Mongo profiler and applies container CPU/RAM
caps, `benchmark` creates throwaway workspaces it means to delete. Interrupted, all
of that outlives the process -- a workspace that silently is not serving, a rate
limiter left off, ten pods still running -- with nothing anywhere recording that it
happened. `web/jobs.py` keeps its registry in memory, so a restart loses even the
knowledge that a job existed.

Cooperative cancellation cannot fix this. It helps a graceful shutdown, which the
25-second drain in `jobs.drain` already mostly covers; the dangerous exit is the one
where no Python runs at all. What survives that is a note written BEFORE the risky
change and removed AFTER it is undone.

THE HARD PART IS NOT WRITING IT, IT IS KNOWING WHOSE IT IS. An open entry belongs
either to a job still running or to a process that died. Repairing the first would
re-enable the rate limiter underneath a load test that is still going. So each entry
records the pid AND that process's start time, and recovery touches an entry only
when its owner is provably gone -- pid alone is not enough, because the OS recycles
them (the same mistake `forward_alive` was making about port-forwards).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rc_repro import config, runner
from rc_repro.services.events import Emit, info, null_emit, warn

#: One file per open side effect. A directory rather than a single log because
#: entries are created and cleared concurrently by unrelated jobs, and an unlink is
#: atomic where a rewrite of a shared file is a lost-update race.
DIRNAME = "journal"

#: Kinds, closed on purpose: recovery has to know how to undo each one, so a kind
#: nothing can repair is a note that would sit there forever looking like a fault.
RATE_LIMITER_OFF = "rate_limiter_off"
ROCKETCHAT_STOPPED = "rocketchat_stopped"
#: A throwaway workspace `benchmark` means to delete. The most expensive of these to
#: leave behind: one Rocket.Chat plus one MongoDB per version under test.
BENCH_WORKSPACE = "bench_workspace"
#: Container CPU/RAM caps a performance run applied, with the PRIOR values so restore
#: puts back what was there rather than guessing at "unlimited".
CONSTRAINTS_APPLIED = "constraints_applied"
#: A create that never reached the end. ADVISORY -- see `ADVISORY` below.
CREATE_UNFINISHED = "create_unfinished"
#: Rocket.Chat's Prometheus metrics endpoint, turned on for a `--diag` run. Restored in
#: a `finally`, which is exactly the block a SIGKILL does not run.
RC_METRICS_ON = "rc_metrics_on"
#: Mongo's query profiler, armed at the caller's `slowms` for a `--diag` run. Carries
#: the PRIOR level, because `mongoprof.stop` restores to a value rather than to off --
#: a workspace that already had profiling on must keep it.
#:
#: The worse of the two to leave behind: level 1 writes a system.profile document for
#: every operation over slowms, for the life of the workspace, which is a permanent
#: write amplification on somebody's reproduction that then LOOKS like a Rocket.Chat
#: performance problem.
MONGO_PROFILER_ON = "mongo_profiler_on"
#: Email-2FA turned off for the duration of a seed. Seeded users have no mailbox on a
#: repro, so seeding disables this to make them loginable and restores it in a `finally`
#: -- the block a SIGKILL does not run. A `large` seed is minutes long and `jobs.drain`
#: gives it 25s, so a `systemctl restart` mid-seed is routine rather than exotic; what
#: survives is a workspace whose 2FA posture silently differs from the one an engineer
#: is about to measure.
EMAIL_2FA_OFF = "email_2fa_off"
#: The database was DROPPED and the archive had not finished loading. ADVISORY: recovery
#: cannot safely re-run a mongorestore at `serve` startup -- it would be minutes of work
#: on a workspace nobody asked about, and re-running it against a partially loaded
#: database is not obviously better than leaving it. So it is reported and left, and
#: `doctor`'s `interrupted-work` row picks it up for free.
#:
#: The window is real: `--drop` is not enough on its own (a collection absent from the
#: bundle would survive), so the restore drops the database FIRST -- and a multi-GB
#: bundle then takes minutes. A SIGKILL in there restarts Rocket.Chat against an empty
#: database, and `_Quiesced`'s own note makes recovery restart it, so `serve` reports the
#: workspace repaired. README promises "the target database is dropped first, so you
#: never get a hybrid" -- true, and it swapped in a worse failure that nothing recorded.
DATABASE_DROPPED = "database_dropped"
KINDS = (RATE_LIMITER_OFF, ROCKETCHAT_STOPPED, BENCH_WORKSPACE,
         CONSTRAINTS_APPLIED, CREATE_UNFINISHED, RC_METRICS_ON, MONGO_PROFILER_ON,
         EMAIL_2FA_OFF, DATABASE_DROPPED)
#: The setting EMAIL_2FA_OFF is about, named once so the seed path and the repair cannot
#: disagree about which key they mean.
EMAIL_2FA_SETTING = "Accounts_TwoFactorAuthentication_By_Email_Enabled"

#: Kinds recovery REPORTS but does not undo by itself.
#:
#: `CREATE_UNFINISHED` is the whole set, and the reason is a timing one: finishing a
#: create means waiting for Rocket.Chat to serve and then running the preset's
#: self-configuration, which can take the full readiness timeout. Doing that inside
#: `serve`'s startup would hold the GUI shut for minutes on a workspace nobody asked
#: about yet. `rc-repro ready --name <it>` is the same work on demand, and it clears
#: the note when it succeeds -- so this is a note that closes itself once acted on,
#: not one that sits there forever.
ADVISORY = (CREATE_UNFINISHED, DATABASE_DROPPED)


@dataclass
class Entry:
    id: str
    kind: str
    workspace: str
    pid: int
    started: str = ""          # the owning process's start time, as /proc reports it
    at: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def owner_alive(self) -> bool:
        """Whether the process that wrote this is still running.

        pid AND start time, because a recycled pid is a different process. An
        unknowable start time (no /proc) falls back to pid liveness, which is the
        conservative direction here: it means "assume the owner is alive", so
        recovery leaves the entry alone rather than undoing live work.
        """
        current = proc_started(self.pid)
        if current is None:
            return False
        if not self.started:
            return True
        return current == self.started


def journal_dir() -> Path:
    return config.home() / DIRNAME


def proc_started(pid: int) -> str | None:
    """A process's start time as a stable string, or None if it is not running.

    Field 22 of /proc/<pid>/stat, in clock ticks since boot. Read from the raw
    bytes and split from the RIGHT of the last ')', because field 2 is the
    executable name and may itself contain spaces or brackets.
    """
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_bytes().decode("utf-8", "replace")
    except (OSError, ValueError, TypeError):
        return None
    try:
        return raw[raw.rindex(")") + 1:].split()[19]
    except (ValueError, IndexError):
        return ""          # running, but the start time could not be read


def record(kind: str, workspace: str, **detail) -> str:
    """Note a side effect that must be undone. Returns the entry id.

    Best-effort by design: a journal that cannot be written must not stop the
    operation it was describing. The operation's own `finally` is still the primary
    cleanup -- this is only what covers the case where no `finally` runs.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown journal kind {kind!r} (want: {', '.join(KINDS)})")
    entry = Entry(id=secrets.token_hex(8), kind=kind, workspace=workspace,
                  pid=os.getpid(), started=proc_started(os.getpid()) or "",
                  # UTC, LIKE EVERY OTHER TIMESTAMP HERE. `time.strftime` with no
                  # argument is LOCAL time, so one event read 22:29 in a journal note
                  # and 16:59 in the repro.json written beside it -- and a note is read
                  # next to `doctor`, `list` and an audit line, all of which are UTC.
                  at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                  detail=dict(detail))
    try:
        d = journal_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        runner.atomic_write(d / f"{entry.id}.json", json.dumps(asdict(entry), indent=2))
    except OSError:
        return ""
    return entry.id


def clear(entry_id: str) -> None:
    """Forget an entry, because whatever it described has been undone."""
    if not entry_id:
        return
    try:
        (journal_dir() / f"{entry_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def clear_kind(kind: str, workspace: str) -> int:
    """Forget EVERY note of one kind for one workspace. Returns how many went.

    Some facts invalidate other processes' notes, not just your own. "This workspace's
    create finished" is one: a create that failed left a `CREATE_UNFINISHED` note
    behind, and a later create of the same name that succeeded cleared only the note it
    had written itself -- so the stale one went on claiming a complete workspace had
    never finished. Seen exactly that way, when a failed attempt was followed by a good
    one.
    """
    gone = 0
    for entry in open_entries():
        if entry.kind == kind and entry.workspace == workspace:
            clear(entry.id)
            gone += 1
    return gone


@contextmanager
def side_effect(kind: str, workspace: str, **detail):
    """Record on the way in, clear on the way out -- however that happens.

    Wraps an existing `finally` rather than replacing it: the note is cleared when
    the normal cleanup has run, so a surviving entry means precisely "the cleanup did
    not happen".
    """
    entry_id = record(kind, workspace, **detail)
    try:
        yield entry_id
    finally:
        clear(entry_id)


def open_entries() -> list[Entry]:
    """Every entry on disk, newest last. Unreadable files are skipped, not raised."""
    out: list[Entry] = []
    try:
        paths = sorted(journal_dir().glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(Entry(id=str(data.get("id") or path.stem),
                             kind=str(data.get("kind") or ""),
                             workspace=str(data.get("workspace") or ""),
                             pid=int(data.get("pid") or 0),
                             started=str(data.get("started") or ""),
                             at=str(data.get("at") or ""),
                             detail=dict(data.get("detail") or {})))
        except (OSError, ValueError, TypeError):
            continue
    return out


def abandoned() -> list[Entry]:
    """Entries whose owning process is gone -- the ones safe to act on."""
    return [e for e in open_entries() if not e.owner_alive]


def describe(entry: Entry) -> str:
    """One line a human can act on, whether or not recovery can."""
    if entry.kind == RATE_LIMITER_OFF:
        return (f"{entry.workspace!r}: the API rate limiter was turned off for a "
                f"performance run at {entry.at} and never turned back on")
    if entry.kind == ROCKETCHAT_STOPPED:
        return (f"{entry.workspace!r}: Rocket.Chat was stopped for a backup at "
                f"{entry.at} and never started again")
    if entry.kind == BENCH_WORKSPACE:
        return (f"{entry.workspace!r}: a throwaway benchmark workspace from {entry.at} "
                f"is still here, holding a Rocket.Chat and a MongoDB")
    if entry.kind == CONSTRAINTS_APPLIED:
        caps = ", ".join(str(a.get("service") or "?")
                         for a in (entry.detail.get("applied") or [])[:3])
        return (f"{entry.workspace!r}: CPU/RAM limits from a performance run at "
                f"{entry.at} are still applied ({caps or 'unknown services'})")
    if entry.kind == DATABASE_DROPPED:
        return (f"{entry.workspace!r}: a restore from "
                f"{entry.detail.get('bundle') or 'a bundle'} dropped the database at "
                f"{entry.at} and never finished loading it — the workspace is running "
                f"against an EMPTY or partial database. `rc-repro restore "
                f"{entry.detail.get('bundle') or '<bundle>'} --name {entry.workspace}` "
                f"again is what completes it")
    if entry.kind == EMAIL_2FA_OFF:
        return (f"{entry.workspace!r}: email two-factor authentication was turned off "
                f"for a seed at {entry.at} and never turned back on — seeded users are "
                f"loginable and real ones may not be")
    if entry.kind == RC_METRICS_ON:
        return (f"{entry.workspace!r}: Rocket.Chat's Prometheus metrics endpoint was "
                f"turned on for a diagnostic run at {entry.at} and never turned off")
    if entry.kind == MONGO_PROFILER_ON:
        return (f"{entry.workspace!r}: Mongo's query profiler was armed for a "
                f"diagnostic run at {entry.at} and never disarmed — it is still "
                f"writing a system.profile document for every slow operation")
    if entry.kind == CREATE_UNFINISHED:
        seeded = " and its seed" if entry.detail.get("seed") else ""
        return (f"{entry.workspace!r}: the create started at {entry.at} never "
                f"finished, so it may be running WITHOUT its preset configuration"
                f"{seeded or ' or seed data'} — "
                f"`rc-repro ready --name {entry.workspace}` completes it")
    return f"{entry.workspace!r}: {entry.kind} since {entry.at}"


def recover(emit: Emit = null_emit, *, dry_run: bool = False) -> list[dict]:
    """Undo what abandoned entries describe. Returns one result per entry.

    Never raises: this runs at `serve` startup, and a box that cannot be repaired
    still has to be able to serve. Each result carries `repaired` so a caller can
    say what is still outstanding.
    """
    results: list[dict] = []
    for entry in abandoned():
        row = {"id": entry.id, "kind": entry.kind, "workspace": entry.workspace,
               "what": describe(entry), "repaired": False, "why": ""}
        if entry.kind in ADVISORY and not runner.exists(entry.workspace):
            # The workspace is gone, so an unfinished create is no longer anybody's
            # problem. Advisory notes are never repaired, so without this they would
            # warn forever about something that cannot be acted on.
            #
            # UNDER dry_run THIS IS A READ. The clear used to sit above the dry_run
            # check, so a dry run over two notes reported one and DELETED the other --
            # and a dry run is the only way to inspect the journal without repairing
            # it, which makes mutating one a contradiction in terms.
            if dry_run:
                row["why"] = "workspace is gone; would be dropped"
                results.append(row)
                continue
            clear(entry.id)
            continue
        if dry_run or entry.kind in ADVISORY:
            # Reported and left alone. See ADVISORY: undoing this one means waiting for
            # a workspace to serve, which is not something a GUI startup may spend.
            row["why"] = ("needs `rc-repro ready`" if entry.kind in ADVISORY else "")
            results.append(row)
            if entry.kind in ADVISORY:
                warn(emit, row["what"], phase="preflight")
            continue
        try:
            row["repaired"] = _repair(entry, emit)
        except Exception as exc:  # noqa: BLE001 - recovery must not break startup
            row["why"] = str(exc)
        if row["repaired"]:
            clear(entry.id)
            info(emit, f"repaired: {row['what']}", phase="preflight")
        else:
            warn(emit, f"could NOT repair — {row['what']}"
                       + (f" ({row['why']})" if row["why"] else ""), phase="preflight")
        results.append(row)
    return results


def _repair(entry: Entry, emit: Emit) -> bool:
    """Undo one entry. False when it could not be done and the note should stay."""
    from rc_repro.services import lifecycle

    if not runner.exists(entry.workspace):
        # The workspace is gone, so whatever was done to it went with it. Clearing
        # the note is the correct outcome, not a failure to repair.
        return True
    if entry.kind == BENCH_WORKSPACE:
        # `benchmark` creates these and removes them in its own `finally`; the whole
        # point of the note is the run that never got there.
        runner.down(entry.workspace, volumes=True)
        runner.remove(entry.workspace)
        return True
    if entry.kind == CONSTRAINTS_APPLIED:
        from rc_repro.perf import constrain as constrain_mod
        applied = [constrain_mod.Applied(**a)
                   for a in (entry.detail.get("applied") or [])]
        # `restore` returns problems rather than raising, so an empty list is success.
        return not constrain_mod.restore(applied)
    if entry.kind == RATE_LIMITER_OFF:
        from rc_repro import rcapi
        meta = runner.read_meta(entry.workspace)
        auth = lifecycle.login(meta)
        if auth is None:
            return False
        return bool(rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD,
                                     config.RC_RATE_LIMITER_SETTING, True))
    if entry.kind == EMAIL_2FA_OFF:
        from rc_repro import rcapi
        meta = runner.read_meta(entry.workspace)
        auth = lifecycle.login(meta)
        if auth is None:
            return False
        return bool(rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD,
                                      EMAIL_2FA_SETTING, True))
    if entry.kind == RC_METRICS_ON:
        from rc_repro import rcapi
        from rc_repro.services import monitoring
        meta = runner.read_meta(entry.workspace)
        auth = lifecycle.login(meta)
        if auth is None:
            return False
        return bool(rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD,
                                      monitoring.RC_METRICS_SETTING, False))
    if entry.kind == MONGO_PROFILER_ON:
        from rc_repro.perf import mongoprof
        # To the PRIOR level, not to off. `mongoprof.stop` already takes it, and
        # resetting to 0 would silently turn off profiling somebody had enabled
        # themselves -- the same distinction CONSTRAINTS_APPLIED draws by carrying
        # its prior values.
        prior = entry.detail.get("prior")
        if prior is None:
            return False
        return bool(mongoprof.stop(entry.workspace, prior))
    if entry.kind == ROCKETCHAT_STOPPED:
        # BOTH RUNTIMES. `_Quiesced` scales the deployment to 0 on Kubernetes and
        # stops the compose services otherwise, so the note carries whichever one it
        # did and the repair has to match. Returning True for a Kubernetes workspace
        # after doing nothing would clear the note and leave it stopped.
        context = str(entry.detail.get("context") or "")
        if context:
            from rc_repro.services import k8s
            replicas = int(entry.detail.get("replicas") or 1)
            return k8s.scale_rocketchat(entry.workspace, replicas=replicas,
                                        context=context) == 0
        services = list(entry.detail.get("services") or [])
        if not services:
            return False
        return runner.start_services(entry.workspace, services) == 0
    return False


def pending_seed(workspace: str) -> dict:
    """The seed an interrupted create asked for and never ran, or {}.

    Read from the NOTE rather than from the workspace's metadata, because that is the
    only place it survives: the `CreateReq` died with the process, and
    `meta.extra["seed"]` is written only after a seed has already succeeded -- so
    metadata can say "seeded" or say nothing, and "nothing" covers both "none was
    asked for" and "one was asked for and lost".

    Only OPEN notes count. A note whose owner is still alive belongs to a create that
    is still running, and finishing its seed underneath it would seed twice.
    """
    for entry in abandoned():
        if entry.kind == CREATE_UNFINISHED and entry.workspace == workspace:
            if entry.detail.get("seed"):
                return dict(entry.detail)
    return {}

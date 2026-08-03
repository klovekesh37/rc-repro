"""FastAPI application for the local rc-repro GUI.

Imported lazily by `rc-repro serve`. Blocking service calls run in path
operations declared with `def` (Starlette runs those in a threadpool), so the
event loop is never blocked. Long operations become background jobs (see
jobs.py) streamed to the browser over SSE.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from importlib import resources
from pathlib import Path

import subprocess
import threading

from fastapi import (Body, FastAPI, File, Form, Request, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from rc_repro import config
from rc_repro import presets as presets_mod
from rc_repro import runner
from rc_repro.errors import NotReadyError, ReproError, ValidationError
from rc_repro.services import data as datasvc
from rc_repro.services import lifecycle as lc
from rc_repro.web.jobs import JobManager

# `docker compose logs --tail N` is buffered in memory server-side, so a
# caller-supplied N needs a ceiling.
TAIL_MAX = 5000
# An uploaded support dump is read into memory; cap it rather than trusting it.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
# Bounded so a chatty container plus a slow reader can't grow the queue forever.
WS_QUEUE_MAX = 10_000
# What the API-call console may send. A whitelist because `method` reaches
# requests.request() verbatim; the CLI's own examples only use these.
_API_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

_UPLOAD_ID_RE = re.compile(r"^u[0-9a-f]{12}$")


def _clamp_tail(tail: int) -> int:
    try:
        return max(1, min(int(tail), TAIL_MAX))
    except (TypeError, ValueError):
        return 200


def _only_set(only: str) -> set[str] | None:
    """Parse the comma-separated id-prefix filter into a set, or None for 'all'."""
    return {p.strip() for p in (only or "").split(",") if p.strip()} or None


def _prune_uploads(dest: Path, keep: int = 5) -> None:
    """Keep only the newest `keep` uploads.

    A previewed-but-never-applied dump has nothing to delete it, and these are
    customers' configuration files — they should not accumulate indefinitely.
    """
    uploads = sorted(dest.glob("u*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in uploads[keep:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".only").unlink(missing_ok=True)


def _read_upload(file: UploadFile) -> bytes:
    """Read an upload with a hard cap (it used to be an unbounded .read())."""
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"settings file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    return data


def create_app(token: str = "", allow_hosts: list[str] | None = None) -> FastAPI:
    # openapi_url=None as well as the doc UIs: the schema path does not start
    # with /api/, so `guard` below would hand it out without a token.
    app = FastAPI(title="rc-repro", docs_url=None, redoc_url=None, openapi_url=None)
    jobs = JobManager()
    app.state.token = token

    # Host allow-list (DNS-rebind/CSRF guard). Loopback always allowed; extra
    # hosts (e.g. a reverse-proxy domain like *.iximiuz.com) opt in via
    # --allow-host, and "*" trusts any Host.
    # Case-folded on both sides: hostnames are case-insensitive, and comparing
    # raw meant `curl http://LOCALHOST:7070/` and a proxy that forwards
    # `Host: Lab.Example.Com` were both rejected as "host not allowed".
    allowed = {h.lower() for h in ({"localhost", "127.0.0.1", "::1"} | set(allow_hosts or []))}
    any_host = "*" in (allow_hosts or [])

    def _hostname(hdr: str | None) -> str:
        """A Host header's bare hostname: port stripped, lowercased.

        Handles bracketed IPv6 ("[::1]:7070" -> "::1"), which a plain
        split(":") would mangle to "[" — making the "::1" entry unmatchable.
        """
        h = (hdr or "").strip()
        if h.startswith("["):
            return h[1:].split("]", 1)[0].lower()
        return h.split(":", 1)[0].lower()

    def host_ok(hdr: str | None) -> bool:
        # A missing/empty Host is rejected rather than allowed: "" must not be a
        # member of the allow-list, or any Host-less request slips past it.
        return any_host or _hostname(hdr) in allowed
    app.state.host_ok = host_ok

    # Defence in depth for the SPA. script-src 'self' blocks inline handlers, so an
    # injected `<img onerror=...>` cannot run even if a renderer forgets to escape.
    # Styles need 'unsafe-inline' because the UI sets style ATTRIBUTES; frame-src
    # allows the monitoring stack's Grafana, which the k6 result embeds.
    csp = ("default-src 'self'; "
           "script-src 'self'; "
           "style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data:; "
           "connect-src 'self' ws: wss:; "
           f"frame-src 'self' http://localhost:{config.MONITOR_PORTS[1]}; "
           "base-uri 'none'; form-action 'none'; object-src 'none'")

    # --- security: Host allow-list + token on the API
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not host_ok(request.headers.get("host")):
            return JSONResponse({"error": "host not allowed (use serve --allow-host)"}, status_code=403)
        path = request.url.path
        if token and path.startswith("/api/") and path != "/api/health":
            given = request.headers.get("x-rc-repro-token") or request.query_params.get("t")
            if given != token:
                return JSONResponse({"error": "bad or missing token"}, status_code=401)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # The session token rides in ?t= (EventSource/WebSocket cannot set
        # headers), so suppress the Referer that would carry it off-origin.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(ReproError)
    async def _repro_error(_: Request, exc: ReproError):
        return JSONResponse({"error": str(exc), "kind": type(exc).__name__},
                            status_code=exc.http_status)

    # --- read (blocking -> def -> threadpool) ---------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "docker": runner.docker_available()}

    @app.get("/api/repros")
    def list_repros():
        return {"repros": lc.list_repros()}

    @app.get("/api/doctor")
    def doctor():
        """The same preflight checks as `rc-repro doctor`.

        The dashboard's docker badge could only say up/down; when it said down,
        every card reported "docker unavailable — actions disabled" and offered no
        diagnosis. This is the click-through.
        """
        from rc_repro.services import doctor as doctorsvc
        return doctorsvc.run_checks()

    @app.get("/api/repros/{name}")
    def describe(name: str):
        return lc.describe(name)

    @app.get("/api/presets")
    def list_presets():
        return {"presets": [
            {"name": p.name, "description": p.description, "params_help": p.params_help,
             "requires_license": p.requires_license} for p in presets_mod.list_presets()]}

    @app.get("/api/versions/{version}")
    def resolve_version(version: str, offline: bool = False):
        """Resolve an RC version to its MongoDB pairing WITHOUT launching anything.

        Lets the create dialog show the pairing before the user commits to a
        multi-gigabyte image pull, and pre-empts the trap that otherwise only
        surfaces minutes in as a mongod crash.
        """
        from rc_repro import versions as versions_mod
        try:
            r = versions_mod.resolve(version, offline=offline)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        out = {"rc_version": r.rc_version, "rc_image": r.rc_image, "mongo_tag": r.mongo_tag,
               "mongo_flavor": r.mongo_flavor, "mongo_shell": r.mongo_shell,
               "oplog": r.oplog, "source": r.source, "note": r.note, "kernel": ""}
        # SERVER-121912: mongod 8.0 hard-exits on kernel >= 6.19, and the failure
        # reads like a volume/permission problem. Say so before the pull, not after.
        kv = runner.docker_kernel_version()
        out["kernel"] = kv or ""
        mm = re.match(r"(\d+)\.(\d+)", kv or "")
        try:
            mongo_major = int(r.mongo_tag.split(".")[0])
        except ValueError:
            mongo_major = 0
        if mm and mongo_major >= 8 and (int(mm.group(1)), int(mm.group(2))) >= (6, 19):
            out["warning"] = (f"this engine's kernel ({kv}) cannot run MongoDB 8.0 "
                              "(SERVER-121912) — mongod will exit on boot")
        return out

    @app.get("/api/repros/{name}/detail")
    def detail(name: str):
        return lc.detail(name)

    @app.get("/api/repros/{name}/stats")
    def stats(name: str):
        from rc_repro.perf import resources as R
        target = lc.resolve_name(name)
        ids = runner.container_ids(target)
        prefix = f"{config.PROJECT_PREFIX}{target}-"
        cpu = mem = 0.0
        for line in runner.docker_stats(ids).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            # Strip the `rcrepro-<name>-` prefix before matching. A substring test
            # against the full container name meant a repro NAMED e.g.
            # "rocketchat-slow" matched every container, silently summing Mongo and
            # every sidecar into the RC chart.
            svc = parts[0][len(prefix):] if parts[0].startswith(prefix) else parts[0]
            if svc == "rocketchat" or svc.startswith("rocketchat-"):
                cpu += R._parse_cpu(parts[1])
                used, _ = R._parse_mem(parts[2])
                mem += used
        return {"cpu": round(cpu, 1), "mem_mb": round(mem / 1e6, 1)}

    @app.websocket("/api/repros/{name}/logs/stream")
    async def logs_stream(ws: WebSocket, name: str, tail: int = 300):
        # WS bypasses the http middleware, so enforce host + token here.
        if not app.state.host_ok(ws.headers.get("host")):
            await ws.close(code=1008); return
        if token and ws.query_params.get("t") != token:
            await ws.close(code=1008); return
        await ws.accept()
        try:
            target = lc.resolve_name(name)
        except ReproError as exc:
            await ws.send_json({"error": str(exc)}); await ws.close(); return

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        proc = subprocess.Popen(
            ["docker", "compose", "logs", "-f", "--no-color",
             "--tail", str(_clamp_tail(tail))],
            cwd=runner.workspace(target), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)

        def offer(line: str | None) -> None:
            """Enqueue on the loop thread, dropping lines when the reader falls
            behind — but never dropping the end-of-stream sentinel."""
            if line is not None:
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    pass
                return
            while True:
                try:
                    q.put_nowait(None)
                    return
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        return

        def pump():
            for line in proc.stdout or []:
                loop.call_soon_threadsafe(offer, line.rstrip("\n"))
            loop.call_soon_threadsafe(offer, None)

        threading.Thread(target=pump, daemon=True).start()

        async def watch_client() -> None:
            """Completes when the client goes away.

            Without it the handler blocks on q.get() forever for a QUIET
            container: the send that would raise never happens, so the
            `docker compose logs -f` child and the pump thread outlive the
            browser tab indefinitely.
            """
            try:
                while True:
                    await ws.receive()
            except Exception:  # noqa: BLE001 - a disconnect of any flavour
                return

        watcher = asyncio.create_task(watch_client())
        try:
            while True:
                getter = asyncio.ensure_future(q.get())
                done, _pending = await asyncio.wait(
                    {getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    getter.cancel()
                    break
                line = getter.result()
                if line is None:
                    break
                await ws.send_text(line)
        except WebSocketDisconnect:
            pass
        finally:
            watcher.cancel()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    @app.get("/api/repros/{name}/logs")
    def logs(name: str, tail: int = 200):
        target = lc.resolve_name(name)
        lines: list[str] = []
        runner.compose_stream(target, "logs", "--no-color",
                              "--tail", str(_clamp_tail(tail)),
                              on_line=lines.append)
        return {"name": target, "logs": "\n".join(lines)}

    # --- mutating ------------------------------------------------------------
    @app.post("/api/repros")
    def create(req: dict = Body(...)):
        allowed = set(lc.CreateReq.__dataclass_fields__)
        creq = lc.CreateReq(**{k: v for k, v in req.items() if k in allowed})
        job = jobs.submit("create", lc.create_repro, creq, stream_output=True,
                          label=creq.name or creq.version)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/state")
    def state(name: str, body: dict = Body(...)):
        lc.set_state(name, body.get("action", ""))
        return {"ok": True}

    @app.post("/api/repros/{name}/up")
    def bring_up(name: str):
        """Recreate a `down`ed repro's containers from its stored metadata.

        `docker compose start` cannot revive a repro that was `down`ed — there are
        no containers left to start — so /state is useless for it. This is the
        GUI's equivalent of the CLI's `up --version <same> --name <same>`: the
        workspace and repro.json survive a `down`, so nothing needs re-entering.
        offline=True because the stored version needs no fresh lookup.
        """
        target = lc.resolve_name(name)
        meta = runner.read_meta(target)
        req = lc.CreateReq(version=meta.rc_version, preset=meta.preset,
                           name=target, wait=True, offline=True)
        job = jobs.submit("up", lc.create_repro, req, stream_output=True, label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/ready")
    def ready(name: str):
        meta = runner.read_meta(lc.resolve_name(name))
        job = jobs.submit("ready", lc.wait_and_finalize, meta, label=meta.name)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/seed")
    def seed(name: str, body: dict = Body(default={})):
        meta = runner.read_meta(lc.resolve_name(name))
        job = jobs.submit("seed", lc.run_seed_inline, meta,
                          body.get("profile", "small"), bool(body.get("stats", False)),
                          label=meta.name)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/scale")
    def scale(name: str, body: dict = Body(...)):
        target = lc.resolve_name(name)
        job = jobs.submit("scale", datasvc.run_scale, target, body.get("scale", ""), label=target)
        return {"job_id": job.id}

    @app.delete("/api/repros/{name}/scale")
    def clear_scale(name: str):
        target = lc.resolve_name(name)
        job = jobs.submit("clear-scale", datasvc.clear_scale, target, label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/config-import/plan")
    def config_import_plan(name: str, file: UploadFile = File(...), only: str = Form("")):
        target = lc.resolve_name(name)
        dest = runner.workspace(target) / "import"
        dest.mkdir(parents=True, exist_ok=True)
        # One file per upload, not a single shared settings.json: two tabs
        # previewing different dumps raced, and the second silently won the
        # first's apply.
        upload_id = "u" + uuid.uuid4().hex[:12]
        (dest / f"{upload_id}.json").write_bytes(_read_upload(file))
        # Pin the filter TO the upload. `apply` used to re-read `only` from its own
        # request, so editing the field after previewing silently applied a different
        # plan than the one that was reviewed — which defeats having a preview.
        (dest / f"{upload_id}.only").write_text(only, encoding="utf-8")
        _prune_uploads(dest)
        onlyset = _only_set(only)
        plan = datasvc.import_plan(target, str(dest / f"{upload_id}.json"), only=onlyset)
        plan["upload_id"] = upload_id
        return plan

    def _import_then_delete(target: str, path: str, onlyset, emit) -> dict:
        """Apply the plan, then remove the uploaded dump — it is a customer's
        configuration and was previously left in the workspace forever."""
        try:
            return datasvc.import_apply(target, path, onlyset, emit=emit)
        finally:
            Path(path).unlink(missing_ok=True)
            Path(path).with_suffix(".only").unlink(missing_ok=True)

    @app.post("/api/repros/{name}/config-import")
    def config_import_apply(name: str, body: dict = Body(default={})):
        target = lc.resolve_name(name)
        upload_id = str(body.get("upload_id") or "")
        # Pattern-checked before it becomes a filename.
        if not _UPLOAD_ID_RE.match(upload_id):
            return JSONResponse({"error": "missing or malformed upload_id - preview the plan first",
                                 "kind": "ValidationError"}, status_code=400)
        path = runner.workspace(target) / "import" / f"{upload_id}.json"
        if not path.exists():
            return JSONResponse({"error": "no uploaded settings.json - preview the plan first",
                                 "kind": "ValidationError"}, status_code=400)
        # The filter is whatever the PREVIEW used, not whatever this request says:
        # what was reviewed is what gets applied. `body["only"]` is ignored.
        onlyfile = path.with_suffix(".only")
        onlyset = _only_set(onlyfile.read_text(encoding="utf-8") if onlyfile.exists() else "")
        job = jobs.submit("config-import", _import_then_delete, target, str(path), onlyset,
                          label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/loadtest")
    def loadtest(name: str, body: dict = Body(default={})):
        from rc_repro.services import perf as perfsvc
        target = lc.resolve_name(name)
        fields = set(perfsvc.LoadtestReq.__dataclass_fields__) - {"name"}
        req = perfsvc.LoadtestReq(name=target, **{k: v for k, v in body.items() if k in fields})
        job = jobs.submit("loadtest", perfsvc.run_loadtest, req, label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/capacity")
    def capacity(name: str, body: dict = Body(default={})):
        from rc_repro.services import perf as perfsvc
        target = lc.resolve_name(name)
        fields = set(perfsvc.CapacityReq.__dataclass_fields__) - {"name"}
        req = perfsvc.CapacityReq(name=target, **{k: v for k, v in body.items() if k in fields})
        job = jobs.submit("capacity", perfsvc.run_capacity, req, label=target)
        return {"job_id": job.id}

    @app.post("/api/benchmark")
    def benchmark(body: dict = Body(...)):
        from rc_repro.services import perf as perfsvc
        vers = body.get("versions") or []
        if isinstance(vers, str):
            vers = [v.strip() for v in vers.split(",") if v.strip()]
        job = jobs.submit("benchmark", perfsvc.run_benchmark, vers,
                          body.get("seed_profile", "standard"),
                          bool(body.get("offline", False)), bool(body.get("no_pull", False)),
                          label=", ".join(vers))
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/monitor")
    def monitor(name: str, off: bool = False):
        from rc_repro.services import monitor as monitorsvc
        target = lc.resolve_name(name)
        job = jobs.submit("monitor-off" if off else "monitor",
                          monitorsvc.detach if off else monitorsvc.attach, target,
                          label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/default")
    def set_default(name: str):
        """Make this repro the default (the CLI's `rc-repro use`).

        The dashboard displayed the `default` pill but had no way to move it --
        the create dialog's Pin checkbox was the only path, so changing it meant
        recreating a repro or dropping to the CLI.
        """
        target = lc.resolve_name(name)
        config.update_config(lambda cfg: cfg.__setitem__("default_repro", target))
        return {"ok": True, "default": target}

    @app.post("/api/repros/{name}/pat")
    def create_pat(name: str, body: dict = Body(default={})):
        """Mint a Personal Access Token and return ready-to-use API headers.

        Synchronous rather than a job: it is two HTTP calls against a repro that
        is already serving, and the caller wants the value back.
        """
        from rc_repro import rcapi
        lc.require_docker()
        meta = runner.read_meta(lc.resolve_name(name))
        label = str(body.get("label") or "rc-repro")
        bypass_2fa = bool(body.get("bypass_2fa", True))
        try:
            auth = lc.login(meta)
            # Not `token` -- that name is create_app's server auth token, and
            # rebinding it here reads like the handler is overwriting it.
            pat = rcapi.generate_pat(meta.root_url, auth, config.ADMIN_PASSWORD,
                                     token_name=label, bypass_2fa=bypass_2fa)
        except Exception as exc:  # noqa: BLE001 - surface as a 409, not a 500
            raise NotReadyError(
                f"could not create a token (is it ready? `rc-repro ready -n {meta.name}`): {exc}"
            ) from exc
        if not pat:
            raise NotReadyError("Rocket.Chat did not return a token (is it ready?)")
        return {"token": pat, "user_id": auth.user_id, "label": label,
                "bypass_2fa": bypass_2fa, "root_url": meta.root_url}

    @app.get("/api/repros/{name}/tls")
    def tls_state(name: str):
        """What the repro is ACTUALLY serving over TLS — the GUI's `tls-status`.

        `up --wait` only proves Rocket.Chat booted: it polls the internal http port.
        Traefik obtains its certificate in the background afterwards and falls back
        to a self-signed placeholder when ACME fails, so HTTPS needs its own check.
        """
        from rc_repro import tls as tlsmod
        meta = runner.read_meta(lc.resolve_name(name))
        if not meta.public_url:
            raise ValidationError(f"{meta.name!r} was not created with --https")
        mode = str(meta.extra.get("tls") or "")
        host = meta.public_url.split("://", 1)[1].split(":")[0]
        port = int((meta.extra.get("tls_ports") or [443])[0])
        cafile = (str(tlsmod.ca_dir() / tlsmod.CA_CRT)
                  if mode == tlsmod.MODE_LOCAL else None)
        # Dial THIS host with the domain as SNI: probing the public name lets a
        # proxy in front answer instead, and its certificate is not ours.
        out = tlsmod.verify("127.0.0.1", port, cafile=cafile, sni=host)
        out.update(mode=mode, public_url=meta.public_url, name=meta.name)
        if mode == tlsmod.MODE_ACME:
            pub = tlsmod.verify(host, 443)
            out["public_issuer"] = pub["issuer"]
            out["public_error"] = pub["error"]
        return out

    @app.post("/api/repros/{name}/call")
    def api_call(name: str, body: dict = Body(...)):
        """One authenticated REST call against a repro -- the GUI's `rc-repro api`.

        A non-2xx *from Rocket.Chat* is a result, not a failure of this endpoint:
        it comes back 200 with the status in the payload, so the panel can show
        "HTTP 403" and the response body the way the CLI prints them. Only being
        unable to make the call at all (bad input, no auth, no connection) is an
        error status here.

        Not an open proxy: rcapi.call() builds `root_url + "/" + path.lstrip("/")`,
        so the host is always this repro's own. A `path` of "http://elsewhere/x"
        or "//elsewhere/x" lands as a path segment on the workspace, not a new
        destination.
        """
        import requests

        from rc_repro import rcapi
        lc.require_docker()
        meta = runner.read_meta(lc.resolve_name(name))

        method = str(body.get("method") or "GET").upper()
        if method not in _API_METHODS:
            raise ValidationError(
                f"unsupported method {method!r} (use {', '.join(_API_METHODS)})")
        path = str(body.get("path") or "").strip()
        if not path:
            raise ValidationError("no API path given, e.g. /api/v1/me")
        raw = str(body.get("data") or "").strip()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ValidationError(f"request body is not valid JSON: {exc}") from exc

        use_pat = bool(body.get("pat"))
        two_fa = bool(body.get("two_fa"))
        try:
            auth = lc.login(meta)
            if use_pat:
                # Same swap the CLI's --pat does: exercise the workspace the way a
                # customer script does, through a token rather than a login session.
                auth = rcapi.Auth(
                    token=rcapi.generate_pat(meta.root_url, auth, config.ADMIN_PASSWORD,
                                             bypass_2fa=True),
                    user_id=auth.user_id)
        except Exception as exc:  # noqa: BLE001 - surface as a 409, not a 500
            raise NotReadyError(
                f"could not authenticate (is it ready? `rc-repro ready -n {meta.name}`): {exc}"
            ) from exc

        extra = rcapi.password_2fa_headers(config.ADMIN_PASSWORD) if two_fa else None
        started = time.monotonic()
        try:
            status, text = rcapi.call(meta.root_url, method, path,
                                      auth=auth, data=payload, extra_headers=extra)
        except requests.RequestException as exc:
            raise NotReadyError(f"request failed: {exc}") from exc
        tag = ("PAT" if use_pat else "admin") + ("+2fa" if two_fa else "")
        return {"status": status, "text": text, "tag": tag,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "url": f"{meta.root_url.rstrip('/')}/{path.lstrip('/')}"}

    @app.delete("/api/repros/{name}")
    def teardown(name: str, volumes: bool = False, confirm: bool = False):
        return lc.teardown(name, volumes=volumes, confirm=confirm)

    @app.post("/api/prune")
    def prune(body: dict = Body(default={})):
        return lc.prune(confirm=bool(body.get("confirm", False)))

    # --- jobs ----------------------------------------------------------------
    @app.get("/api/jobs")
    def jobs_list():
        """Retained jobs, newest first. A job outlives the dialog that started it
        (and any page refresh), so without this the output of a long benchmark or
        capacity search was computed and then unreachable."""
        return {"jobs": jobs.list_jobs()}

    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return {"id": job.id, "kind": job.kind, "status": job.status,
                "result": job.result, "error": job.error, "error_kind": job.error_kind,
                "n_events": job.n_events}

    @app.get("/api/jobs/{job_id}/stream")
    async def job_stream(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)

        async def gen():
            idx = since
            while True:
                # The next index comes from the job, not from counting: a flood
                # trims the oldest events, so the absolute index can jump.
                evs, done, nxt = job.snapshot(idx)
                base = nxt - len(evs)
                for n, e in enumerate(evs):
                    yield f"id: {base + n}\ndata: {json.dumps(e)}\n\n"
                idx = nxt
                if done and not evs:
                    break
                await asyncio.sleep(0.2)
        # no-cache / no-transform keep reverse proxies (the case --allow-host
        # exists for) from buffering the stream into uselessness.
        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })

    # --- static SPA ----------------------------------------------------------
    webui = resources.files("rc_repro").joinpath("data", "webui")
    app.mount("/", StaticFiles(directory=str(webui), html=True), name="ui")
    return app

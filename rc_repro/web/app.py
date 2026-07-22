"""FastAPI application for the local rc-repro GUI.

Imported lazily by `rc-repro serve`. Blocking service calls run in path
operations declared with `def` (Starlette runs those in a threadpool), so the
event loop is never blocked. Long operations become background jobs (see
jobs.py) streamed to the browser over SSE.
"""

from __future__ import annotations

import asyncio
import json
from importlib import resources

import subprocess
import threading

from fastapi import (Body, FastAPI, File, Form, Request, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from rc_repro.errors import ReproError as _ReproError

from rc_repro import presets as presets_mod
from rc_repro import runner
from rc_repro.errors import ReproError
from rc_repro.services import data as datasvc
from rc_repro.services import lifecycle as lc
from rc_repro.web.jobs import JobManager


def create_app(token: str = "", allow_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="rc-repro", docs_url=None, redoc_url=None)
    jobs = JobManager()
    app.state.token = token

    # Host allow-list (DNS-rebind/CSRF guard). Loopback always allowed; extra
    # hosts (e.g. a reverse-proxy domain like *.iximiuz.com) opt in via
    # --allow-host, and "*" trusts any Host.
    allowed = {"localhost", "127.0.0.1", "::1", ""} | set(allow_hosts or [])
    any_host = "*" in (allow_hosts or [])

    def host_ok(hdr: str | None) -> bool:
        return any_host or (hdr or "").split(":")[0] in allowed
    app.state.host_ok = host_ok

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
        return await call_next(request)

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

    @app.get("/api/repros/{name}")
    def describe(name: str):
        return lc.describe(name)

    @app.get("/api/presets")
    def list_presets():
        return {"presets": [
            {"name": p.name, "description": p.description, "params_help": p.params_help,
             "requires_license": p.requires_license} for p in presets_mod.list_presets()]}

    @app.get("/api/repros/{name}/detail")
    def detail(name: str):
        return lc.detail(name)

    @app.get("/api/repros/{name}/stats")
    def stats(name: str):
        from rc_repro.perf import resources as R
        target = lc.resolve_name(name)
        ids = runner.container_ids(target)
        cpu = mem = 0.0
        for line in runner.docker_stats(ids).splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and ("rocketchat" in parts[0]):
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
        except _ReproError as exc:
            await ws.send_json({"error": str(exc)}); await ws.close(); return

        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue()
        proc = subprocess.Popen(
            ["docker", "compose", "logs", "-f", "--no-color", "--tail", str(tail)],
            cwd=runner.workspace(target), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)

        def pump():
            for line in proc.stdout or []:
                loop.call_soon_threadsafe(q.put_nowait, line.rstrip("\n"))
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                line = await q.get()
                if line is None:
                    break
                await ws.send_text(line)
        except WebSocketDisconnect:
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    @app.get("/api/repros/{name}/logs")
    def logs(name: str, tail: int = 200):
        target = lc.resolve_name(name)
        lines: list[str] = []
        runner.compose_stream(target, "logs", "--no-color", "--tail", str(tail),
                              on_line=lines.append)
        return {"name": target, "logs": "\n".join(lines)}

    # --- mutating ------------------------------------------------------------
    @app.post("/api/repros")
    def create(req: dict = Body(...)):
        allowed = set(lc.CreateReq.__dataclass_fields__)
        creq = lc.CreateReq(**{k: v for k, v in req.items() if k in allowed})
        job = jobs.submit("create", lc.create_repro, creq, stream_output=True)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/state")
    def state(name: str, body: dict = Body(...)):
        lc.set_state(name, body.get("action", ""))
        return {"ok": True}

    @app.post("/api/repros/{name}/ready")
    def ready(name: str):
        meta = runner.read_meta(lc.resolve_name(name))
        job = jobs.submit("ready", lc.wait_and_finalize, meta)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/seed")
    def seed(name: str, body: dict = Body(default={})):
        meta = runner.read_meta(lc.resolve_name(name))
        job = jobs.submit("seed", lc.run_seed_inline, meta,
                          body.get("profile", "small"), bool(body.get("stats", False)))
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/scale")
    def scale(name: str, body: dict = Body(...)):
        target = lc.resolve_name(name)
        job = jobs.submit("scale", datasvc.run_scale, target, body.get("scale", ""))
        return {"job_id": job.id}

    @app.delete("/api/repros/{name}/scale")
    def clear_scale(name: str):
        target = lc.resolve_name(name)
        job = jobs.submit("clear-scale", datasvc.clear_scale, target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/config-import/plan")
    def config_import_plan(name: str, file: UploadFile = File(...), only: str = Form("")):
        target = lc.resolve_name(name)
        dest = runner.workspace(target) / "import"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "settings.json").write_bytes(file.file.read())
        onlyset = {p.strip() for p in only.split(",") if p.strip()} or None
        return datasvc.import_plan(target, str(dest / "settings.json"), only=onlyset)

    @app.post("/api/repros/{name}/config-import")
    def config_import_apply(name: str, body: dict = Body(default={})):
        target = lc.resolve_name(name)
        path = str(runner.workspace(target) / "import" / "settings.json")
        if not runner.workspace(target).joinpath("import", "settings.json").exists():
            return JSONResponse({"error": "no uploaded settings.json - preview the plan first",
                                 "kind": "ValidationError"}, status_code=400)
        only = body.get("only") or ""
        onlyset = {p.strip() for p in only.split(",") if p.strip()} or None
        job = jobs.submit("config-import", datasvc.import_apply, target, path, onlyset)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/loadtest")
    def loadtest(name: str, body: dict = Body(default={})):
        from rc_repro.services import perf as perfsvc
        target = lc.resolve_name(name)
        fields = set(perfsvc.LoadtestReq.__dataclass_fields__) - {"name"}
        req = perfsvc.LoadtestReq(name=target, **{k: v for k, v in body.items() if k in fields})
        job = jobs.submit("loadtest", perfsvc.run_loadtest, req)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/capacity")
    def capacity(name: str, body: dict = Body(default={})):
        from rc_repro.services import perf as perfsvc
        target = lc.resolve_name(name)
        fields = set(perfsvc.CapacityReq.__dataclass_fields__) - {"name"}
        req = perfsvc.CapacityReq(name=target, **{k: v for k, v in body.items() if k in fields})
        job = jobs.submit("capacity", perfsvc.run_capacity, req)
        return {"job_id": job.id}

    @app.post("/api/benchmark")
    def benchmark(body: dict = Body(...)):
        from rc_repro.services import perf as perfsvc
        vers = body.get("versions") or []
        if isinstance(vers, str):
            vers = [v.strip() for v in vers.split(",") if v.strip()]
        job = jobs.submit("benchmark", perfsvc.run_benchmark, vers,
                          body.get("seed_profile", "standard"),
                          bool(body.get("offline", False)), bool(body.get("no_pull", False)))
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/monitor")
    def monitor(name: str, off: bool = False):
        from rc_repro.services import monitor as monitorsvc
        target = lc.resolve_name(name)
        job = jobs.submit("monitor-off" if off else "monitor",
                          monitorsvc.detach if off else monitorsvc.attach, target)
        return {"job_id": job.id}

    @app.delete("/api/repros/{name}")
    def teardown(name: str, volumes: bool = False, confirm: bool = False):
        return lc.teardown(name, volumes=volumes, confirm=confirm)

    @app.post("/api/prune")
    def prune(body: dict = Body(default={})):
        return lc.prune(confirm=bool(body.get("confirm", False)))

    # --- jobs ----------------------------------------------------------------
    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return {"id": job.id, "kind": job.kind, "status": job.status,
                "result": job.result, "error": job.error, "error_kind": job.error_kind,
                "n_events": len(job.events)}

    @app.get("/api/jobs/{job_id}/stream")
    async def job_stream(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)

        async def gen():
            idx = since
            while True:
                evs, done = job.snapshot(idx)
                for e in evs:
                    yield f"id: {idx}\ndata: {json.dumps(e)}\n\n"
                    idx += 1
                if done and not evs:
                    break
                await asyncio.sleep(0.2)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- static SPA ----------------------------------------------------------
    webui = resources.files("rc_repro").joinpath("data", "webui")
    app.mount("/", StaticFiles(directory=str(webui), html=True), name="ui")
    return app

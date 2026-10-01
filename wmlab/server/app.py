"""FastAPI backend of the desktop app (ARCHITECTURE.md §9).

``create_app(workspace, token, sync)`` builds the application; ``serve(...)`` runs it with
uvicorn on 127.0.0.1. List routes read the SQLite index, detail routes read the files.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from wmlab import __version__
from wmlab.curriculum import PhasePlan
from wmlab.registry import REGISTRIES, PluginReport, load_plugins, reload_plugins
from wmlab.spec import (
    ExperimentSpec,
    OverrideError,
    SpecError,
    WorkspaceSettings,
    dump_yaml,
    read_yaml,
    save_hosts,
    write_text_atomic,
)
from wmlab.tracking import CheckpointNotFoundError, RunReader, StudyReader, atomic_write_json
from wmlab.server import catalog, costs
from wmlab.server.catalog import FileAccessError
from wmlab.server.context import ServerContext
from wmlab.server.db import IndexDB
from wmlab.server.executors import Runner
from wmlab.server.gitops import GitRepo, GitSync
from wmlab.server.indexer import Indexer, Snapshot
from wmlab.server.jobs import SAFE_ID, JobError, JobManager
from wmlab.server.leaderboard import leaderboard, project_best
from wmlab.server import hostops
from wmlab.server.scaleway import ScalewayClient
from wmlab.server.auth import (
    DOWNLOAD_HEADERS,
    FORBIDDEN_DETAIL,
    INLINE_MEDIA_TYPES,
    MEMBER_RUN_FILE_PREFIX,
    BodyLimitMiddleware,
    PUBLIC_PATHS,
    UNAUTHORIZED_DETAIL,
    audit,
    current_user,
    identify,
    is_member,
    member_denied,
    redact_dataset,
    redact_host,
    redact_job,
    redact_run_detail,
    redact_run_summary,
    redact_study_detail,
    register_auth_errors,
    register_auth_routes,
    require_admin,
)
from wmlab.users import UserStore
from wmlab.server.views import (
    as_float,
    checkpoint_view,
    env_view,
    epoch_of,
    eval_view,
    event_view,
    last_activity,
    opt_str,
    param_importances,
    pareto_front,
    run_summary,
    sample_view,
    str_dict,
    study_summary,
    trial_stats,
    trial_view,
)

log = logging.getLogger("wmlab.server")

RUN_EVENTS_LIMIT = 2000
MAX_POINTS = 5000          # upper bound of ?max_points= (metrics, system, compare)
COMPARE_MAX_RUNS = 20
MIN_TOKEN_LEN = 16         # explicit server token outside the loopback
RECENT_RUNS = 10
RECENT_JOBS = 10


# -- request bodies -----------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str
    title: str = ""
    modality: str
    message_bits: int = 48
    description: str = ""


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    from_: str | None = Field(None, alias="from")


class HpoCreate(BaseModel):
    name: str
    experiment: str


class FileWrite(BaseModel):
    path: str
    content: str


class ValidateBody(BaseModel):
    content: str
    kind: str | None = None
    path: str | None = None


class NotesBody(BaseModel):
    notes: str


class TagsBody(BaseModel):
    tags: list[str]


class InitFrom(BaseModel):
    run: str
    checkpoint: str = "last"
    start_phase: str | int | None = None
    resume: bool = False


class JobCreate(BaseModel):
    kind: str
    host: str | None = None
    spec_path: str | None = None
    run_id: str | None = None
    checkpoint: str | None = None
    benchmark: str | None = None
    overrides: dict[str, Any] | None = None
    max_hours: float | None = None  # NaN/inf are refused by JobManager.submit (400)
    smoke: bool = False
    init_from: InitFrom | None = None
    git_ref: str | None = None


class DecommissionBody(BaseModel):
    confirm: bool = False
    delete_instance: bool = False
    force: bool = False


class EstimateBody(BaseModel):
    spec_path: str
    host: str | None = None
    overrides: dict[str, Any] | None = None


# -- middleware ---------------------------------------------------------------------------------
class TokenMiddleware:
    """Authenticate ``/api/*`` and apply the role policy (§9, §12.2).

    The ``X-WMLab-Token`` header carries the server token (implicit ``admin``) or a session
    token of an active account; ``?token=`` is only read on the two download routes. Public
    routes (health, login, register) pass without identity; everything else answers 401.
    A ``member`` is read-only: non-GET requests (except logout / password) and the GET routes
    that describe the code or the infrastructure answer 403. ``scope["wmlab_authenticated"]``
    and ``scope["wmlab_user"]`` (``{id, name, email, role, auth, session_id}``) are set for
    the routes.
    """

    def __init__(self, app: Any, token: str | None, users: UserStore | None = None) -> None:
        self.app = app
        self.token = token
        self.users = users

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket" and str(scope.get("path", "")).startswith("/api/"):
            await send({"type": "websocket.close", "code": 1008})  # no WebSocket API
            return
        if (scope["type"] != "http" or scope.get("method") == "OPTIONS"
                or not scope["path"].startswith("/api/")):
            await self.app(scope, receive, send)
            return
        user = identify(scope, self.token, self.users)
        scope["wmlab_authenticated"] = user is not None
        scope["wmlab_user"] = user
        path, method = scope["path"], str(scope.get("method") or "GET").upper()
        if path in PUBLIC_PATHS:  # health, login, register: whoever calls them
            await self.app(scope, receive, send)
            return
        if user is None:
            response = JSONResponse({"detail": UNAUTHORIZED_DETAIL}, status_code=401)
            await response(scope, receive, send)
            return
        if user.get("role") != "admin" and member_denied(method, path):
            audit("forbidden", user=user.get("name"), method=method, path=path)
            response = JSONResponse({"detail": FORBIDDEN_DETAIL}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# -- server state -------------------------------------------------------------------------------
class ServerState:
    """Components shared by the routes."""

    def __init__(self, ctx: ServerContext, token: str | None, *, sync: bool, background: bool,
                 runner: Runner | None, cli_prefix: list[str] | None,
                 remote_cli_prefix: list[str] | None, poll_interval: float,
                 index_interval: float) -> None:
        self.ctx = ctx
        self.token = token
        self.background = background
        self.index_interval = index_interval
        self.db = IndexDB(ctx.index_path)
        self.indexer = Indexer.for_context(ctx, self.db)
        self.repo = GitRepo(ctx.workspace, ctx.data)
        self.gitsync = GitSync(self.repo, self.settings)
        self.users = UserStore(ctx.data)
        self.jobs = JobManager(ctx, runner=runner, cli_prefix=cli_prefix,
                               remote_cli_prefix=remote_cli_prefix, sync=sync,
                               poll_interval=poll_interval, repo=self.repo,
                               snapshot=self.snapshot, reindex=self.indexer.scan)
        self.scaleway_transport: Any = None  # injectable (tests: httpx.MockTransport)
        self._plugins: PluginReport | None = None
        self._plugins_lock = threading.Lock()
        self._importances: dict[str, tuple[str, dict[str, float] | None]] = {}

    def start(self) -> None:
        if not self.background:
            return
        threading.Thread(target=self.plugins, name="wmlab-plugins", daemon=True).start()
        self.indexer.start(self.index_interval)
        self.jobs.start()
        self.gitsync.start()

    def stop(self) -> None:
        self.gitsync.stop()
        self.indexer.stop()
        self.jobs.stop()

    def hosted(self) -> bool:
        return bool(self.settings().hosted.enabled)

    def scaleway(self) -> ScalewayClient | None:
        """Client from ``SCW_SECRET_KEY`` (``None`` when the key is not set)."""
        return ScalewayClient.from_env(transport=self.scaleway_transport)

    def plugins(self, reload: bool = False) -> PluginReport:
        """Load builtin components and ``lab/`` plugins (once, or again on ``reload``).

        A reload forgets the imported ``lab`` modules (and their registry entries) first so
        that edited plugin files are imported again.
        """
        with self._plugins_lock:
            if reload:
                self._plugins = reload_plugins(self.ctx.workspace)
            elif self._plugins is None:
                self._plugins = load_plugins(self.ctx.workspace)
            return self._plugins

    def snapshot(self) -> Snapshot:
        return self.indexer.snapshot()

    def settings(self) -> WorkspaceSettings:
        return self.ctx.settings()

    def importances(self, study_dir: Path, trials: list[dict[str, Any]],
                    space: dict[str, Any] | None, directions: list[str]
                    ) -> dict[str, float] | None:
        try:
            st = (study_dir / "trials.jsonl").stat()
            key = f"{st.st_mtime_ns}:{st.st_size}:{','.join(directions)}"
        except OSError:
            key = "none"
        cached = self._importances.get(study_dir.name)
        if cached is not None and cached[0] == key:
            return cached[1]
        value = param_importances(trials, space, directions)
        self._importances[study_dir.name] = (key, value)
        return value


# -- helpers ------------------------------------------------------------------------------------
def _setup_logging(logs_dir: Path) -> None:
    logger = logging.getLogger("wmlab.server")
    logger.setLevel(logging.INFO)
    path = str((logs_dir / "server.log").resolve())
    for handler in list(logger.handlers):
        if getattr(handler, "_wmlab_server", False):
            if getattr(handler, "baseFilename", None) == path:
                return
            logger.removeHandler(handler)
            handler.close()
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s"))
    handler._wmlab_server = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def _run_dir(state: ServerState, run_id: str) -> Path:
    if not SAFE_ID.match(run_id) or ".." in run_id:
        raise HTTPException(400, f"Identifiant de run invalide : {run_id!r}")
    folder = state.ctx.runs_dir / run_id
    if not (folder / "run.json").is_file():
        raise HTTPException(404, f"Run introuvable : {run_id}")
    return folder


def _study_dir(state: ServerState, study_id: str) -> Path:
    if not SAFE_ID.match(study_id) or ".." in study_id:
        raise HTTPException(400, f"Identifiant d'étude invalide : {study_id!r}")
    folder = state.ctx.studies_dir / study_id
    if not (folder / "study.json").is_file():
        raise HTTPException(404, f"Étude introuvable : {study_id}")
    return folder


def _project_dir(state: ServerState, name: str) -> Path:
    folder = catalog.project_dir(state.ctx.workspace, name)
    if folder is None:
        raise HTTPException(404, f"Projet introuvable : {name}")
    return folder


def _created_key(m: dict[str, Any]) -> tuple[float, str]:
    return (epoch_of(m.get("created_at")) or epoch_of(m.get("started_at")) or 0.0,
            str(m.get("run_id") or m.get("study_id") or ""))


def _plan(spec: dict[str, Any]) -> PhasePlan | None:
    try:
        return PhasePlan(ExperimentSpec.model_validate(spec))
    except Exception:
        return None


def _csv_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _cost_items(state: ServerState, snap: Snapshot) -> list[dict[str, Any]]:
    return costs.cost_items(snap.runs, snap.studies, snap.evals, state.settings())


def _running_rate(state: ServerState, snap: Snapshot) -> float:
    """€/h of running runs/studies plus running jobs not visible in the index yet."""
    running_runs = {m.get("run_id") for m in snap.runs if m.get("status") == "running"}
    running_studies = {m.get("study_id") for m in snap.studies if m.get("status") == "running"}
    extra = 0.0
    try:
        hosts = state.jobs.hosts()
    except Exception:
        hosts = {}
    for job in state.jobs.list("running"):
        host = hosts.get(job.host)
        if host is None or job.kind in ("setup", "retrieve"):
            continue
        if job.kind == "train" and job.run_id in running_runs:
            continue
        if job.kind == "hpo" and job.study_id in running_studies:
            continue
        extra += host.price_eur_per_hour
    return costs.running_rate(snap.runs, snap.studies, extra)


def _project_summary(folder: Path, snap: Snapshot, cost_by_project: dict[str, float]
                     ) -> dict[str, Any]:
    name = folder.name
    data = catalog.project_data(folder)
    objectives = catalog.project_objectives(data)
    runs = [m for m in snap.runs if m.get("project") == name]
    studies = [m for m in snap.studies if m.get("project") == name]
    evals = [e for e in snap.evals if e.get("project") == name]
    return {
        "name": name,
        "title": str(data.get("title") or name),
        "modality": str(data.get("modality") or "image"),
        "description": str(data.get("description") or ""),
        "message_bits": int(data.get("message_bits") or 48),
        "default_benchmark": opt_str(data.get("default_benchmark")),
        "tags": [str(t) for t in data.get("tags") or []],
        "objectives": objectives,
        "n_experiments": len(catalog.yaml_files(folder / "experiments")),
        "n_hpo_plans": len(catalog.yaml_files(folder / "hpo")),
        "n_runs": sum(1 for m in runs if not m.get("study")),
        "n_running": sum(1 for m in runs if m.get("status") == "running"),
        "last_activity": last_activity(runs + studies),
        "cost_eur": round(cost_by_project.get(name, 0.0), 2),
        "best": project_best(objectives, data.get("default_benchmark"), runs, evals),
    }


def _cost_by_project(items: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        if item.get("project"):
            out[item["project"]] = out.get(item["project"], 0.0) + item["eur"]
    return out


def _projects(state: ServerState, snap: Snapshot) -> list[dict[str, Any]]:
    by_project = _cost_by_project(_cost_items(state, snap))
    return [_project_summary(folder, snap, by_project)
            for folder in catalog.project_dirs(state.ctx.workspace)]


def _run_detail(run_dir: Path) -> dict[str, Any]:
    reader = RunReader(run_dir)
    try:
        manifest = reader.manifest()
    except FileNotFoundError:
        raise HTTPException(404, f"Run introuvable : {run_dir.name}") from None
    manifest["run_id"] = run_dir.name
    spec = reader.spec()
    plan = _plan(spec)
    return {
        **run_summary(manifest),
        "spec": spec,
        "spec_yaml": reader.spec_yaml(),
        "spec_path": opt_str(manifest.get("spec_path")),
        "env": env_view(reader.env()),
        "plan": plan.preview() if plan is not None else None,
        "events": [event_view(e) for e in reader.events(limit=RUN_EVENTS_LIMIT)],
        "checkpoints": [checkpoint_view(c) for c in reader.checkpoints()],
        "evals": [eval_view(e) for e in reader.evals()],
        "notes": reader.notes(),
        "samples": [sample_view(s) for s in reader.samples()],
        "metric_keys": reader.metric_keys(),
        "price_eur_per_hour": as_float(manifest.get("price_eur_per_hour")),
        "datasets": str_dict(manifest.get("datasets")),
        "overrides": str_dict(manifest.get("overrides")),
    }


def _merge_ordered(existing: Any, new: Any) -> Any:
    """Replace values while keeping the key order of ``existing`` (new keys appended)."""
    if isinstance(existing, dict) and isinstance(new, dict):
        merged = {k: _merge_ordered(existing[k], new[k]) for k in existing if k in new}
        merged.update({k: v for k, v in new.items() if k not in merged})
        return merged
    return new


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        msg = str(err.get("msg", ""))
        if err.get("type") == "missing":
            msg = "champ requis"
        parts.append(f"{loc} : {msg}" if loc else msg)
    return "Requête invalide — " + " ; ".join(parts)


# -- application --------------------------------------------------------------------------------
def create_app(workspace: Path, token: str | None, sync: bool = True, *,
               background: bool = True, runner: Runner | None = None,
               cli_prefix: list[str] | None = None, remote_cli_prefix: list[str] | None = None,
               poll_interval: float = 2.0, index_interval: float = 5.0) -> FastAPI:
    """Build the FastAPI app of ``workspace``.

    ``sync`` enables the periodic rsync of remote jobs; ``background=False`` disables every
    thread (index, jobs, plugins) — routes then refresh on demand (tests). ``runner`` and
    ``cli_prefix``/``remote_cli_prefix`` are injectable for tests.
    """
    ctx = ServerContext.for_workspace(Path(workspace))
    os.environ["WMLAB_WORKSPACE"] = str(ctx.workspace)
    _setup_logging(ctx.logs_dir)
    state = ServerState(ctx, token, sync=sync, background=background, runner=runner,
                        cli_prefix=cli_prefix, remote_cli_prefix=remote_cli_prefix,
                        poll_interval=poll_interval, index_interval=index_interval)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("serveur démarré (workspace %s, données %s)", ctx.workspace, ctx.data)
        state.start()
        try:
            yield
        finally:
            state.stop()
            log.info("serveur arrêté")

    app = FastAPI(title="Watermark Lab", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.wmlab = state
    # Run details (git_diff, spec_yaml, events) reach a few hundred kB and are polled while a
    # run is live: compress every response above 1 kB when the client accepts gzip.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(TokenMiddleware, token=token, users=state.users)
    app.add_middleware(BodyLimitMiddleware)
    if token is not None:  # cross-origin access (Vite dev server) is only safe with a token
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                           allow_headers=["*"], expose_headers=["Retry-After"])
    _register_errors(app, state)
    register_auth_errors(app)
    register_auth_routes(app, state)
    _register_routes(app, state)
    dist = ctx.workspace / "desktop" / "dist"
    if (dist / "index.html").is_file():
        app.mount("/", _DesktopFiles(directory=str(dist), html=True), name="desktop")
    return app


#: Security headers of the served frontend (§12.7; Caddy adds HSTS on top). The CSP matches the
#: Vite build: external hashed scripts and styles, inline ``style`` attributes (ECharts,
#: CodeMirror), same-origin API, media and sample images (``blob:`` for client-side downloads).
SPA_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
        "object-src 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class _DesktopFiles(StaticFiles):
    """``desktop/dist`` with cache and security headers: ``index.html`` is revalidated on every
    load (a rebuild must not be masked by a cached page) while the hashed ``assets/`` are
    immutable; every page carries ``SPA_SECURITY_HEADERS``."""

    def file_response(self, full_path: os.PathLike[str] | str, stat_result: os.stat_result,
                      scope: Any, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        name = Path(full_path)
        if name.name == "index.html":
            response.headers["Cache-Control"] = "no-cache"
        elif name.parent.name == "assets":
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        for header, value in SPA_SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


def _register_errors(app: FastAPI, state: ServerState) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def request_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, _validation_message(exc))

    @app.exception_handler(JobError)
    async def job_error(_: Request, exc: JobError) -> JSONResponse:
        return _error(exc.status, exc.message)

    @app.exception_handler(FileAccessError)
    async def file_error(_: Request, exc: FileAccessError) -> JSONResponse:
        return _error(exc.status, exc.message)

    @app.exception_handler(SpecError)
    async def spec_error(_: Request, exc: SpecError) -> JSONResponse:
        return _error(422, str(exc))

    @app.exception_handler(OverrideError)
    async def override_error(_: Request, exc: OverrideError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(FileExistsError)
    async def exists_error(_: Request, exc: FileExistsError) -> JSONResponse:
        return _error(409, str(exc))

    @app.exception_handler(CheckpointNotFoundError)
    async def checkpoint_error(_: Request, exc: CheckpointNotFoundError) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(FileNotFoundError)
    async def missing_file(_: Request, exc: FileNotFoundError) -> JSONResponse:
        if state.hosted():  # never a server path towards a browser
            log.info("fichier introuvable : %s", exc)
            return _error(404, "Fichier introuvable ou illisible.")
        return _error(404, f"Fichier introuvable ou illisible : {exc}")

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception) -> JSONResponse:
        ref = secrets.token_hex(4)
        log.exception("erreur interne (réf. %s)", ref)
        if state.hosted():  # the details (paths, versions) stay in server.log
            return _error(500, f"Erreur interne du serveur (réf. {ref}) : consultez le journal "
                               "du serveur.")
        return _error(500, f"Erreur interne du serveur : {type(exc).__name__}: {exc}")


def _register_routes(app: FastAPI, state: ServerState) -> None:  # noqa: C901 - route table
    ctx = state.ctx
    ws = ctx.workspace

    # -- health & overview ---------------------------------------------------------------------
    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        hosted = state.settings().hosted
        body: dict[str, Any] = {"ok": True, "version": __version__,
                                "hosted": bool(hosted.enabled),
                                "registration": bool(hosted.allow_registration)}
        if request.scope.get("wmlab_authenticated", True) and not is_member(request):
            body["workspace"] = str(ws)  # server path: admins only (CORS is open)
        return body

    # identity and accounts (§12.2): /api/me, /api/auth/*, /api/users* — wmlab.server.auth

    # -- git (§12.3) ---------------------------------------------------------------------------
    @app.get("/api/git")
    def git_state(request: Request) -> dict[str, Any]:
        require_admin(request)
        return state.repo.state(state.hosted())

    @app.post("/api/git/pull")
    def git_pull(request: Request) -> dict[str, Any]:
        require_admin(request)
        if not state.repo.available:
            raise HTTPException(400, "Aucun dépôt git dans le workspace.")
        if state.repo.dirty():
            raise HTTPException(409, "Arbre de travail modifié (fichiers non commités) : "
                                     "impossible de tirer la branche.")
        branch = state.repo.branch() or state.settings().hosted.git_branch
        error = state.repo.pull(branch)
        if error is not None:
            raise HTTPException(409, f"Pull impossible : {error}")
        return state.repo.state(state.hosted())

    @app.get("/api/overview")
    def overview(request: Request) -> dict[str, Any]:
        member = is_member(request)
        view = redact_run_summary if member else (lambda summary: summary)
        snap = state.snapshot()
        settings = state.settings()
        items = _cost_items(state, snap)
        spent = round(sum(i["eur"] for i in items), 2)
        total = float(settings.budget.total_eur)
        by_project = _cost_by_project(items)
        runs = sorted(snap.runs, key=_created_key, reverse=True)
        if not state.jobs.running:
            state.jobs.tick()
        jobs = state.jobs.list()
        shown_jobs = [j for j in jobs if j.active] + [j for j in jobs if not j.active][:RECENT_JOBS]
        return {
            "workspace": {"name": settings.name, "path": "" if member else str(ws)},
            "budget": {"total_eur": total, "spent_eur": spent,
                       "remaining_eur": round(total - spent, 2),
                       "running_eur_per_hour": round(_running_rate(state, snap), 4)},
            "counts": {
                "projects": len(catalog.project_dirs(ws)),
                "runs": sum(1 for m in snap.runs if not m.get("study")),
                "running": sum(1 for m in snap.runs if m.get("status") == "running"),
                "studies": len(snap.studies),
                "gpu_hours": costs.gpu_hours(items),
            },
            "running": [view(run_summary(m)) for m in runs if m.get("status") == "running"],
            "recent": [view(run_summary(m)) for m in runs if not m.get("study")][:RECENT_RUNS],
            "projects": [_project_summary(folder, snap, by_project)
                         for folder in catalog.project_dirs(ws)],
            "jobs": [redact_job(j.to_api()) if member else j.to_api() for j in shown_jobs],
        }

    # -- projects ------------------------------------------------------------------------------
    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return _projects(state, state.snapshot())

    @app.post("/api/projects")
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        from wmlab.scaffold import ScaffoldError, new_project

        try:
            path = new_project(body.name, body.modality, body.message_bits,
                               title=body.title or None, description=body.description,
                               workspace=ws)
        except ScaffoldError as exc:
            raise HTTPException(400, str(exc)) from None
        log.info("projet créé : %s", path)
        snap = state.snapshot()
        return _project_summary(path.parent, snap,
                                _cost_by_project(_cost_items(state, snap)))

    @app.get("/api/projects/{name}")
    def get_project(name: str) -> dict[str, Any]:
        folder = _project_dir(state, name)
        snap = state.snapshot()
        run_counts: dict[str, int] = {}
        for m in snap.runs:
            if m.get("project") == name and not m.get("study"):
                key = str(m.get("experiment") or "")
                run_counts[key] = run_counts.get(key, 0) + 1
        study_counts: dict[str, int] = {}
        for m in snap.studies:
            if m.get("project") == name:
                key = str(m.get("plan") or "")
                study_counts[key] = study_counts.get(key, 0) + 1
        return {
            "summary": _project_summary(folder, snap,
                                        _cost_by_project(_cost_items(state, snap))),
            "spec": catalog.project_data(folder),
            "experiments": [catalog.experiment_info(ws, p, run_counts)
                            for p in catalog.yaml_files(folder / "experiments")],
            "hpo_plans": [catalog.hpo_info(ws, p, study_counts)
                          for p in catalog.yaml_files(folder / "hpo")],
            "notes": catalog.project_notes(folder),
        }

    @app.post("/api/projects/{name}/experiments")
    def create_experiment(name: str, body: ExperimentCreate) -> dict[str, Any]:
        from wmlab.scaffold import ScaffoldError, new_experiment

        _project_dir(state, name)
        try:
            path = new_experiment(name, body.name, from_experiment=body.from_ or None,
                                  workspace=ws)
        except ScaffoldError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"path": ctx.rel(path)}

    @app.post("/api/projects/{name}/hpo")
    def create_hpo(name: str, body: HpoCreate) -> dict[str, Any]:
        from wmlab.scaffold import ScaffoldError, new_hpo

        _project_dir(state, name)
        try:
            path = new_hpo(name, body.name, body.experiment, workspace=ws)
        except ScaffoldError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"path": ctx.rel(path)}

    # -- files & validation --------------------------------------------------------------------
    @app.get("/api/files")
    def read_file(request: Request, path: str = Query(...)) -> dict[str, Any]:
        require_admin(request)  # the code and the specs are never served to a member
        target, rel = catalog.resolve_workspace_file(ws, path, data_root=ctx.data)
        content = catalog.read_text_file(target)
        kind = catalog.file_kind(target)
        return {"path": rel, "content": content, "kind": kind,
                "validation": catalog.file_validation(target, content, kind)}

    @app.put("/api/files")
    def write_file(body: FileWrite, request: Request) -> dict[str, Any]:
        require_admin(request)
        target, rel = catalog.resolve_workspace_file(ws, body.path, write=True,
                                                     data_root=ctx.data)
        if len(body.content.encode("utf-8")) > catalog.MAX_FILE_BYTES:
            raise HTTPException(413, "Contenu trop volumineux (1 Mo maximum).")
        if target.exists() and not target.is_file():
            raise HTTPException(400, f"« {rel} » est un dossier.")
        if rel.startswith("benchmarks/") and target.is_file():
            # a published benchmark (used by recorded evaluations) is frozen: new version only
            name = target.stem
            used = sum(1 for e in state.snapshot().evals if e.get("benchmark") == name)
            if used:
                raise HTTPException(409, (
                    f"Le benchmark « {name} » est publié : {used} évaluation(s) enregistrée(s) "
                    "l'utilisent et ne seraient plus comparables. Créez une nouvelle version "
                    f"(benchmarks/<modalité>_v<N+1>.yaml) au lieu de le modifier."))
        write_text_atomic(target, body.content)
        audit("file-saved", user=current_user(request)["name"], path=rel)
        kind = catalog.file_kind(target)
        validation = catalog.file_validation(target, body.content, kind)
        if rel.startswith("lab/") and kind == "python" and validation["valid"]:
            try:  # make the edited plugin visible in the registries right away
                state.plugins(reload=True)
            except Exception:
                log.exception("rechargement des plugins impossible")
        git: dict[str, Any] | None = None
        hosted = state.settings().hosted
        if hosted.enabled and hosted.auto_commit and state.repo.available:
            user = current_user(request)["name"]
            git = state.repo.commit_and_push(rel, user, hosted.git_branch)
            if git["error"]:
                log.warning("commit/push de %s par %s : %s", rel, user, git["error"])
            else:
                log.info("fichier %s commité (%s) par %s%s", rel, (git["committed"] or "-")[:7],
                         user, "" if git["pushed"] else " (non poussé)")
        return {"saved": True, "validation": validation, "git": git}

    @app.post("/api/validate")
    def validate(body: ValidateBody) -> dict[str, Any]:
        return catalog.validate_payload(ws, body.content, body.kind, body.path)

    @app.get("/api/registry")
    def registry(request: Request, reload: bool = False) -> dict[str, Any]:
        require_admin(request)
        report = state.plugins(reload=reload)
        return {"entries": {kind: reg.describe() for kind, reg in REGISTRIES.items()},
                "errors": [{"module": str(e.get("module", "")), "error": str(e.get("error", ""))}
                           for e in report.errors]}

    # -- runs ----------------------------------------------------------------------------------
    @app.get("/api/runs")
    def list_runs(request: Request, project: str | None = None, experiment: str | None = None,
                  status: str | None = None, study: str | None = None, host: str | None = None,
                  include_trials: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        view = redact_run_summary if is_member(request) else (lambda summary: summary)
        statuses = set(_csv_list(status))
        runs = []
        for m in state.snapshot().runs:
            if project and m.get("project") != project:
                continue
            if experiment and m.get("experiment") != experiment:
                continue
            if study and m.get("study") != study:
                continue
            if not study and not include_trials and m.get("study"):
                continue
            if host and m.get("host") != host:
                continue
            if statuses and m.get("status") not in statuses:
                continue
            runs.append(m)
        runs.sort(key=_created_key, reverse=True)
        return [view(run_summary(m)) for m in runs[:max(1, min(limit, 10000))]]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        detail = _run_detail(_run_dir(state, run_id))
        return redact_run_detail(detail) if is_member(request) else detail

    @app.get("/api/runs/{run_id}/metrics")
    def run_metrics(run_id: str, keys: str | None = None,
                    max_points: int = 1000) -> dict[str, Any]:
        reader = RunReader(_run_dir(state, run_id))
        plan = _plan(reader.spec())
        return {"series": reader.metrics(_csv_list(keys) or None, max(2, min(max_points, MAX_POINTS))),
                "phases": [{"name": n, "start": s, "end": e} for n, s, e in plan.bounds]
                if plan is not None else []}

    @app.get("/api/runs/{run_id}/system")
    def run_system(run_id: str, max_points: int = 500) -> dict[str, Any]:
        return RunReader(_run_dir(state, run_id)).system(max(2, min(max_points, MAX_POINTS)))

    @app.get("/api/runs/{run_id}/logs")
    def run_logs(run_id: str, request: Request, tail: int = 500) -> dict[str, Any]:
        require_admin(request)  # commands and tracebacks
        return {"lines": RunReader(_run_dir(state, run_id)).logs(max(0, min(tail, 20000)))}

    @app.get("/api/runs/{run_id}/file")
    def run_file(run_id: str, request: Request, path: str = Query(...)) -> FileResponse:
        run_dir = _run_dir(state, run_id).resolve()
        target = (run_dir / path).resolve()
        if not target.is_relative_to(run_dir) or target == run_dir:
            raise HTTPException(403, "Accès refusé : fichier hors du dossier du run.")
        rel = target.relative_to(run_dir).as_posix()
        if is_member(request) and not rel.startswith(MEMBER_RUN_FILE_PREFIX):
            # checkpoints (weights), spec.yaml, logs: never for a read-only member
            raise HTTPException(403, "Accès réservé aux administrateurs : seuls les échantillons "
                                     "du run sont consultables.")
        if not target.is_file():
            raise HTTPException(404, f"Fichier introuvable dans le run : {path}")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if media_type in INLINE_MEDIA_TYPES:
            return FileResponse(target, media_type=media_type, headers=dict(DOWNLOAD_HEADERS))
        # anything else (HTML, SVG, scripts, .pt…) is a download, never a page of this origin
        return FileResponse(target, media_type="application/octet-stream",
                            headers=dict(DOWNLOAD_HEADERS), filename=target.name)

    @app.put("/api/runs/{run_id}/notes")
    def set_notes(run_id: str, body: NotesBody) -> dict[str, Any]:
        RunReader(_run_dir(state, run_id)).set_notes(body.notes)
        return {"ok": True}

    @app.put("/api/runs/{run_id}/tags")
    def set_tags(run_id: str, body: TagsBody) -> dict[str, Any]:
        run_dir = _run_dir(state, run_id)
        reader = RunReader(run_dir)
        try:
            manifest = reader.raw_manifest()
        except FileNotFoundError:
            raise HTTPException(409, f"run.json du run {run_id} illisible (écriture en cours "
                                     "ou fichier corrompu) : réessayez plus tard.") from None
        if reader.manifest().get("status") == "running":
            raise HTTPException(409, "Le run est en cours : son process réécrit run.json, les "
                                     "tags seront modifiables à la fin du run.")
        manifest["tags"] = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
        atomic_write_json(run_dir / "run.json", manifest)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str, request: Request) -> dict[str, Any]:
        _run_dir(state, run_id)
        job = state.jobs.stop_run(run_id)
        audit("run-stopped", user=current_user(request)["name"], run=run_id)
        return job.to_api() if job is not None else {"ok": True}

    @app.post("/api/runs/{run_id}/checkpoints/{name}/fetch")
    def fetch_checkpoint(run_id: str, name: str) -> dict[str, Any]:
        _run_dir(state, run_id)
        state.jobs.fetch_checkpoint(run_id, name)
        return {"ok": True}

    @app.get("/api/compare")
    def compare(request: Request, runs: str = "", keys: str | None = None,
                max_points: int = 500) -> dict[str, Any]:
        view = redact_run_summary if is_member(request) else (lambda summary: summary)
        summaries, series = [], {}
        wanted = _csv_list(keys) or None
        for run_id in list(dict.fromkeys(_csv_list(runs)))[:COMPARE_MAX_RUNS]:
            if not SAFE_ID.match(run_id) or ".." in run_id:
                continue
            run_dir = ctx.runs_dir / run_id
            if not (run_dir / "run.json").is_file():
                continue
            reader = RunReader(run_dir)
            manifest = reader.manifest()
            manifest["run_id"] = run_id
            summaries.append(view(run_summary(manifest)))
            series[run_id] = reader.metrics(wanted, max(2, min(max_points, MAX_POINTS)))
        return {"runs": summaries, "series": series}

    # -- studies -------------------------------------------------------------------------------
    @app.get("/api/studies")
    def list_studies(project: str | None = None) -> list[dict[str, Any]]:
        studies = [m for m in state.snapshot().studies
                   if not project or m.get("project") == project]
        studies.sort(key=_created_key, reverse=True)
        return [study_summary(m, m.get("_stats") or {}) for m in studies]

    @app.get("/api/studies/{study_id}")
    def get_study(study_id: str, request: Request) -> dict[str, Any]:
        study_dir = _study_dir(state, study_id)
        reader = StudyReader(study_dir)
        manifest = reader.manifest()
        manifest["study_id"] = study_id
        spec = reader.spec()
        trials = [trial_view(t) for t in reader.trials()]
        summary = study_summary(manifest, trial_stats(trials), spec)
        directions = [o["direction"] for o in summary["objectives"]]
        space = spec.get("space") if isinstance(spec.get("space"), dict) else None
        detail = {
            **summary,
            "spec": spec,
            "trials": trials,
            "importances": state.importances(study_dir, trials, space, directions),
            "pareto": pareto_front(trials, directions),
            "notes": reader.notes(),
        }
        return redact_study_detail(detail) if is_member(request) else detail

    @app.put("/api/studies/{study_id}/notes")
    def set_study_notes(study_id: str, body: NotesBody) -> dict[str, Any]:
        StudyReader(_study_dir(state, study_id)).set_notes(body.notes)
        return {"ok": True}

    # -- leaderboard, benchmarks, datasets -----------------------------------------------------
    @app.get("/api/leaderboard")
    def get_leaderboard(project: str | None = None, benchmark: str | None = None,
                        source: str | None = None) -> dict[str, Any]:
        if source not in (None, "", "benchmark", "val"):
            raise HTTPException(400, "source invalide — valeurs possibles : benchmark, val")
        snap = state.snapshot()
        return leaderboard(snap.runs, snap.evals, project=project or None,
                           benchmark=benchmark or None, source=source or None)

    @app.get("/api/benchmarks")
    def list_benchmarks() -> list[dict[str, Any]]:
        return [catalog.benchmark_info(ws, p) for p in catalog.yaml_files(ws / "benchmarks")]

    @app.get("/api/datasets")
    def list_datasets(request: Request) -> list[dict[str, Any]]:
        users = catalog.dataset_users(ws)
        infos = [catalog.dataset_info(ws, p, users.get(p.stem, []))
                 for p in catalog.yaml_files(ws / "datasets")]
        return [redact_dataset(i) for i in infos] if is_member(request) else infos

    # -- hosts ---------------------------------------------------------------------------------
    @app.get("/api/hosts")
    def list_hosts(request: Request) -> list[dict[str, Any]]:
        views = [state.jobs.host_view(h) for h in ctx.hosts()]
        return [redact_host(v) for v in views] if is_member(request) else views

    @app.put("/api/hosts")
    def save_hosts_route(body: list[dict[str, Any]], request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        computed = ("unretrieved_checkpoints", "unretrieved_bytes", "inventory_at", "active_jobs")
        cleaned = [{k: v for k, v in h.items() if k not in computed} if isinstance(h, dict) else h
                   for h in body]
        save_hosts(cleaned, ctx.hosts_path)
        audit("hosts-saved", user=current_user(request)["name"], count=len(body))
        return [state.jobs.host_view(h) for h in ctx.hosts()]

    def _known_host(name: str) -> None:
        if name not in state.jobs.hosts():
            raise HTTPException(404, f"Machine inconnue : {name}")

    @app.post("/api/hosts/{name}/test")
    def test_host(name: str) -> dict[str, Any]:
        host = state.jobs.hosts().get(name)
        if host is None:
            raise HTTPException(404, f"Machine inconnue : {name}")
        return state.jobs.executor(host).test()

    @app.post("/api/hosts/{name}/setup")
    def setup_host(name: str, request: Request) -> dict[str, Any]:
        _known_host(name)
        return state.jobs.setup_host(name, current_user(request)["name"]).to_api()

    # -- hosts: inventory, retrieval, decommission (§12.4–12.5) --------------------------------
    @app.get("/api/hosts/{name}/inventory")
    def host_inventory(name: str, request: Request) -> dict[str, Any]:
        require_admin(request)
        _known_host(name)
        return state.jobs.inventory(name)

    @app.post("/api/hosts/{name}/retrieve")
    def host_retrieve(name: str, request: Request) -> dict[str, Any]:
        _known_host(name)
        return state.jobs.retrieve_host(name, current_user(request)["name"]).to_api()

    @app.post("/api/hosts/{name}/decommission")
    def host_decommission(name: str, body: DecommissionBody, request: Request
                          ) -> dict[str, Any]:
        user = require_admin(request)
        _known_host(name)
        if not body.confirm:
            raise HTTPException(400, "Confirmation requise (confirm: true) pour retirer une "
                                     "machine.")
        audit("host-decommission", user=user["name"], host=name, force=body.force,
              delete_instance=body.delete_instance)
        return hostops.decommission(state.jobs, name, delete_instance=body.delete_instance,
                                    force=body.force, user=user["name"],
                                    client_factory=state.scaleway)

    @app.post("/api/hosts/{name}/poweroff")
    def host_poweroff(name: str, request: Request) -> dict[str, Any]:
        user = require_admin(request)
        _known_host(name)
        audit("host-poweroff", user=user["name"], host=name)
        return hostops.poweroff(state.jobs, name, state.scaleway)

    @app.get("/api/hosts/{name}/instance")
    def host_instance(name: str, request: Request) -> dict[str, Any]:
        require_admin(request)
        _known_host(name)
        return hostops.instance(state.jobs, name, state.scaleway)

    # -- jobs ----------------------------------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs(request: Request, status: str | None = None) -> list[dict[str, Any]]:
        if not state.jobs.running:
            state.jobs.tick()
        jobs = [j.to_api() for j in state.jobs.list(status)]
        return [redact_job(j) for j in jobs] if is_member(request) else jobs

    @app.post("/api/jobs")
    def create_job(body: JobCreate, request: Request) -> dict[str, Any]:
        user = current_user(request)["name"]
        job = state.jobs.submit(
            kind=body.kind, host=body.host, spec_path=body.spec_path, run_id=body.run_id,
            checkpoint=body.checkpoint, benchmark=body.benchmark, overrides=body.overrides,
            max_hours=body.max_hours, smoke=body.smoke,
            init_from=body.init_from.model_dump() if body.init_from is not None else None,
            git_ref=body.git_ref, user=user)
        audit("job-created", user=user, job=job.job_id, kind=job.kind, host=job.host,
              target=job.target, git_sha=job.git_sha)
        return job.to_api()

    @app.post("/api/jobs/estimate")
    def estimate_job(body: EstimateBody) -> dict[str, Any]:
        return state.jobs.estimate(body.spec_path, body.host, body.overrides,
                                   state.snapshot().runs)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        job = state.jobs.cancel(job_id)
        audit("job-cancelled", user=current_user(request)["name"], job=job_id)
        return job.to_api()

    @app.post("/api/jobs/{job_id}/sync")
    def sync_job(job_id: str) -> dict[str, Any]:
        return state.jobs.sync_job(job_id).to_api()

    # -- costs & settings ----------------------------------------------------------------------
    @app.get("/api/costs")
    def get_costs() -> dict[str, Any]:
        snap = state.snapshot()
        return costs.costs_view(_cost_items(state, snap), state.settings(),
                                _running_rate(state, snap))

    @app.get("/api/costs.csv")
    def get_costs_csv() -> Response:
        snap = state.snapshot()
        stamp = time.strftime("%Y%m%d")
        return Response(costs.costs_csv(_cost_items(state, snap)),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="watermark-lab-couts-{stamp}.csv"'})

    @app.get("/api/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        require_admin(request)
        return state.settings().model_dump(mode="json")

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any], request: Request) -> dict[str, Any]:
        from wmlab.spec import validate_data

        require_admin(request)
        settings = validate_data(body, "workspace", ctx.settings_path)
        new = settings.model_dump(mode="json")
        existing: Any = {}
        if ctx.settings_path.is_file():
            try:
                existing = read_yaml(ctx.settings_path) or {}
            except SpecError:
                existing = {}
        merged = _merge_ordered(existing if isinstance(existing, dict) else {}, new)
        write_text_atomic(ctx.settings_path, dump_yaml(merged))
        audit("settings-saved", user=current_user(request)["name"])
        hosted = state.settings().hosted
        if hosted.enabled and hosted.auto_commit and state.repo.available:
            # team settings are shared through git like the specs (§12.3); a failed push
            # keeps the local commit (visible as ``ahead`` in GET /api/git)
            user = current_user(request)["name"]
            git = state.repo.commit_and_push(ctx.settings_path.name, user, hosted.git_branch)
            if git["error"]:
                log.warning("commit/push de workspace.yaml par %s : %s", user, git["error"])
        return state.settings().model_dump(mode="json")


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def resolve_token(token: str | None, host: str = "127.0.0.1") -> tuple[str, bool]:
    """Server token (implicit admin): ``--token``, else ``WMLAB_TOKEN``, else a random one.

    Returns ``(token, generated)``. The API is never left open: a generated token is shown
    once on stderr. Listening outside the loopback requires an explicit token.
    """
    explicit = (token or "").strip() or (os.environ.get("WMLAB_TOKEN") or "").strip()
    if explicit:
        if host not in LOOPBACK_HOSTS and len(explicit) < MIN_TOKEN_LEN:
            raise RuntimeError(f"écoute sur {host} : le jeton serveur doit faire au moins "
                               f"{MIN_TOKEN_LEN} caractères (openssl rand -hex 24)")
        return explicit, False
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(f"écoute sur {host} : un jeton explicite est requis (--token ou "
                           "variable WMLAB_TOKEN) pour exposer l'API hors de cette machine")
    return secrets.token_hex(24), True


def serve(port: int = 8765, token: str | None = None, host: str = "127.0.0.1",
          workspace: Path | None = None, sync: bool = True) -> None:
    """Run the backend with uvicorn (blocking). Without ``token``/``WMLAB_TOKEN`` a random
    token is generated and printed once on stderr (the API is never unauthenticated)."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - depends on the environment
        raise RuntimeError("uvicorn n'est pas installé : installez l'extra 'server' "
                           "(fastapi, uvicorn).") from None
    if workspace is None:
        from wmlab.paths import workspace_root

        workspace = workspace_root()
    token, generated = resolve_token(token, host)
    app = create_app(Path(workspace), token, sync)
    if generated:
        print(f"Jeton serveur généré (admin implicite, en-tête X-WMLab-Token) : {token}",
              file=sys.stderr, flush=True)
        log.warning("aucun jeton fourni (--token / WMLAB_TOKEN) : jeton serveur généré")
    if host not in LOOPBACK_HOSTS:
        log.warning("écoute sur %s : l'API n'est protégée que par le jeton", host)
    # proxy_headers / forwarded_allow_ips keep uvicorn's defaults (X-Forwarded-For trusted
    # from 127.0.0.1 only, i.e. the local Caddy); no Server header.
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False,
                server_header=False)

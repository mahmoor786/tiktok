"""Web server: the page, the login and the JSON API behind it."""
from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, jobs, pipeline, work


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    jobs.start()
    work.start_scheduler()
    yield


app = FastAPI(title="Trendpack", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.secret_key(), session_cookie="trendpack",
                   max_age=60 * 60 * 24 * 30, same_site="lax", https_only=False)

CSP = ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def signed_in(request: Request) -> None:
    if not request.session.get("ok"):
        raise HTTPException(401, "Sign in first.")


# ---------------- login ----------------

_attempts: dict[str, deque] = defaultdict(deque)
MAX_ATTEMPTS, WINDOW = 8, 600


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    expected = config.app_password()
    if not expected or expected == "change-me":
        raise HTTPException(503, "No password is set on the server yet. Run setup.sh, or set APP_PASSWORD in the .env file.")
    ip = request.client.host if request.client else "?"
    now, tries = time.time(), _attempts[ip]
    while tries and now - tries[0] > WINDOW:
        tries.popleft()
    if len(tries) >= MAX_ATTEMPTS:
        raise HTTPException(429, "Too many wrong passwords. Wait 10 minutes and try again.")
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        tries.append(now)
        raise HTTPException(401, "That password is not right.")
    tries.clear()
    request.session["ok"] = True
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ---------------- state and settings ----------------

@app.get("/api/state")
def state(request: Request):
    if not request.session.get("ok"):
        return {"signed_in": False}
    settings = db.get_settings()
    now = config.uk_now()
    return {
        "signed_in": True,
        "setup": pipeline.setup_state(settings),
        "categories": [{"id": c["id"], "label": c["label"]} for c in config.CATEGORIES],
        "default_category": settings["default_category"],
        "season": config.season_hint(now),
        "month": now.strftime("%B"),
        "active_jobs": db.active_jobs(),
    }


def _public_settings(settings: dict) -> dict:
    out = {k: v for k, v in settings.items()
           if k not in config.SECRET_SETTINGS and k not in config.INTERNAL_SETTINGS}
    for key in config.SECRET_SETTINGS:
        value = settings.get(key, "")
        out[key] = {"saved": bool(value), "ending": value[-4:] if len(value) > 8 else ""}
    out["setup"] = pipeline.setup_state(settings)
    return out


@app.get("/api/settings", dependencies=[Depends(signed_in)])
def read_settings():
    return _public_settings(db.get_settings())


class SettingsIn(BaseModel):
    gemini_key: str | None = Field(default=None, max_length=200)
    tavily_key: str | None = Field(default=None, max_length=200)
    search_provider: str | None = None
    clip_seconds: int | None = None
    scenes: int | None = None
    default_category: str | None = None
    auto_daily: bool | None = None
    auto_time: str | None = None
    account_notes: str | None = Field(default=None, max_length=2000)


@app.post("/api/settings", dependencies=[Depends(signed_in)])
def write_settings(body: SettingsIn):
    values: dict = {}
    for key in ("gemini_key", "tavily_key"):
        value = getattr(body, key)
        if value is not None and value.strip():
            values[key] = value.strip()
            if key == "gemini_key":
                values["grounding_model"] = ""  # a new key has to be checked again
    if body.search_provider in ("auto", "tavily", "gemini"):
        values["search_provider"] = body.search_provider
    if body.clip_seconds is not None:
        values["clip_seconds"] = max(4, min(body.clip_seconds, 12))
    if body.scenes is not None:
        values["scenes"] = max(1, min(body.scenes, 6))
    if body.default_category and config.category(body.default_category)["id"] == body.default_category:
        values["default_category"] = body.default_category
    if body.auto_daily is not None:
        values["auto_daily"] = "1" if body.auto_daily else "0"
    if body.auto_time is not None:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.auto_time):
            raise HTTPException(400, "The time should look like 07:30.")
        values["auto_time"] = body.auto_time
    if body.account_notes is not None:
        values["account_notes"] = body.account_notes.strip()
    db.set_settings(values)
    return _public_settings(db.get_settings())


@app.post("/api/settings/test", dependencies=[Depends(signed_in)])
def test_settings():
    report = pipeline.self_test(db.get_settings())
    db.set_settings(report.pop("updates"))
    report["settings"] = _public_settings(db.get_settings())
    return report


def _need_setup() -> None:
    if not pipeline.setup_state(db.get_settings())["ready"]:
        raise HTTPException(409, "Add the Gemini and Tavily keys in Settings first, then tap Check connections.")


# ---------------- research ----------------

class ResearchIn(BaseModel):
    category: str = Field(max_length=30)
    note: str = Field(default="", max_length=120)


@app.post("/api/research", dependencies=[Depends(signed_in)])
def new_research(body: ResearchIn):
    _need_setup()
    return work.start_research(body.category, re.sub(r"\s+", " ", body.note).strip())


@app.get("/api/research", dependencies=[Depends(signed_in)])
def research_list():
    return {"research": db.list_research()}


@app.get("/api/research/{research_id}", dependencies=[Depends(signed_in)])
def research_detail(research_id: int):
    research = db.get_research(research_id)
    if not research:
        raise HTTPException(404, "That search no longer exists.")
    research["category_label"] = config.category(research["category"])["label"]
    return research


class PickIn(BaseModel):
    indexes: list[int] = Field(max_length=10)


@app.post("/api/research/{research_id}/packs", dependencies=[Depends(signed_in)])
def packs_from_research(research_id: int, body: PickIn):
    _need_setup()
    research = db.get_research(research_id)
    if not research or research["status"] != "ready":
        raise HTTPException(404, "That search is not ready.")
    result = work.start_packs_from_research(research_id, sorted(set(body.indexes))[:5])
    if not result["pack_ids"]:
        raise HTTPException(400, "Pick at least one product.")
    return result


# ---------------- packs ----------------

class LinkIn(BaseModel):
    url: str = Field(max_length=1000)
    name: str = Field(default="", max_length=120)


@app.post("/api/packs/link", dependencies=[Depends(signed_in)])
def pack_from_link(body: LinkIn):
    _need_setup()
    url = body.url.strip()
    if not re.match(r"^https?://[^\s/]+\.[^\s]+$", url):
        raise HTTPException(400, "Paste the full product link, starting with https://")
    return work.start_pack_from_link(url, re.sub(r"\s+", " ", body.name).strip())


@app.get("/api/packs", dependencies=[Depends(signed_in)])
def pack_list():
    return {"packs": db.list_packs()}


def _pack_or_404(pack_id: int) -> dict:
    pack = db.get_pack(pack_id)
    if not pack:
        raise HTTPException(404, "That pack no longer exists.")
    return pack


@app.get("/api/packs/{pack_id}", dependencies=[Depends(signed_in)])
def pack_detail(pack_id: int):
    return _pack_or_404(pack_id)


class ResultsIn(BaseModel):
    posted: bool = True
    views: int | None = Field(default=None, ge=0, le=2_000_000_000)
    sales: int | None = Field(default=None, ge=0, le=10_000_000)
    hook_used: str = Field(default="", max_length=2)
    notes: str = Field(default="", max_length=300)


@app.post("/api/packs/{pack_id}/results", dependencies=[Depends(signed_in)])
def save_results(pack_id: int, body: ResultsIn):
    _pack_or_404(pack_id)
    db.update("packs", pack_id, {
        "posted": 1 if body.posted else 0, "views": body.views, "sales": body.sales,
        "hook_used": body.hook_used if body.hook_used in ("A", "B") else "",
        "notes": body.notes.strip(), "results_at": db.now_iso()})
    return _pack_or_404(pack_id)


class RetryIn(BaseModel):
    force: bool = False
    name: str = Field(default="", max_length=120)


@app.post("/api/packs/{pack_id}/retry", dependencies=[Depends(signed_in)])
def retry(pack_id: int, body: RetryIn):
    _need_setup()
    pack = _pack_or_404(pack_id)
    if pack["status"] in ("queued", "working"):
        raise HTTPException(409, "This pack is already being made.")
    return work.retry_pack(pack_id, force=body.force, typed_name=re.sub(r"\s+", " ", body.name).strip())


@app.delete("/api/packs/{pack_id}", dependencies=[Depends(signed_in)])
def remove_pack(pack_id: int):
    pack = _pack_or_404(pack_id)
    if pack["status"] in ("queued", "working"):
        raise HTTPException(409, "Wait until this pack has finished before deleting it.")
    db.delete("packs", pack_id)
    return {"ok": True}


# ---------------- jobs ----------------

@app.get("/api/jobs/{job_id}", dependencies=[Depends(signed_in)])
def job(job_id: int):
    found = db.get_job(job_id)
    if not found:
        raise HTTPException(404, "That job no longer exists.")
    return found


# ---------------- the page ----------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

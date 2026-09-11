"""HTTP API for the rally finder."""

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import secrets

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import crawler, db, security, store, telegram
from .matcher import describe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rally")

CRAWL_INTERVAL_MINUTES = int(os.environ.get("CRAWL_INTERVAL_MINUTES", "60"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]

_stop = threading.Event()
_scheduler: BackgroundScheduler | None = None


def run_crawl_and_notify() -> dict:
    crawler.purge_expired()
    summary = crawler.crawl()
    try:
        summary["alerts_sent"] = telegram.notify_matches(summary["new_keys"])
    except Exception:  # noqa: BLE001 - a broken bot must not fail the crawl
        log.exception("notification step failed")
        summary["alerts_sent"] = 0
    return summary


def _startup_crawl() -> None:
    """Crawl at boot only if the data is missing or stale, so restarts are cheap."""
    last = db.get_meta("last_crawl_at")
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if age < timedelta(minutes=CRAWL_INTERVAL_MINUTES):
                log.info("skipping startup crawl, data is %s old", age)
                return
        except ValueError:
            pass
    try:
        run_crawl_and_notify()
    except Exception:  # noqa: BLE001
        log.exception("startup crawl failed; scheduler will retry")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        run_crawl_and_notify,
        "interval",
        minutes=CRAWL_INTERVAL_MINUTES,
        id="crawl",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    _scheduler.start()

    threading.Thread(target=_startup_crawl, daemon=True).start()
    threading.Thread(target=telegram.poll_loop, args=(_stop,), daemon=True).start()

    yield

    _stop.set()
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(
    title="Roadsurfer Rally Finder",
    version="1.0",
    lifespan=lifespan,
    # Caddy only forwards /api/*, so the default /docs and /openapi.json are
    # already unreachable. Disabled anyway so a future routing change cannot
    # publish the schema and the admin endpoint by accident.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(security.BodySizeLimitMiddleware)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _split_ints(value: str | None) -> list[int]:
    out = []
    for v in _split(value):
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out


@app.get("/api/health")
def health():
    return {"status": "ok", "last_crawl_at": db.get_meta("last_crawl_at")}


@app.get("/api/stats")
def stats():
    return store.stats()


@app.get("/api/stations")
def stations(one_way: bool = Query(False, description="Departure stations only")):
    return store.stations(one_way_only=one_way)


@app.get("/api/models")
def models():
    """Camper models seen in any offer, for filter and alert checkboxes."""
    return store.models()


@app.get("/api/offers")
def offers(
    start_stations: str | None = None,
    end_stations: str | None = None,
    countries: str | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    min_days: int | None = None,
    max_days: int | None = None,
    new_hours: int | None = Query(None, description="Only offers first seen within N hours"),
    include_unbookable: bool = Query(
        False,
        description="Also return windows Roadsurfer lists but will not book "
                    "('dead ends'), each tagged with unbookable_reason",
    ),
    limit: int = Query(1000, le=5000),
):
    new_since = None
    if new_hours:
        new_since = (
            datetime.now(timezone.utc) - timedelta(hours=new_hours)
        ).isoformat(timespec="seconds")

    return store.list_offers(
        include_unbookable=include_unbookable,
        start_station_ids=_split_ints(start_stations),
        end_station_ids=_split_ints(end_stations),
        countries=[c.upper() for c in _split(countries)],
        earliest=earliest,
        latest=latest,
        min_days=min_days,
        max_days=max_days,
        new_since=new_since,
        limit=limit,
    )


class WatchIn(BaseModel):
    name: str = Field(default="My search", max_length=120)
    criteria: dict = Field(default_factory=dict)


def _watch_response(watch: dict) -> dict:
    return {
        **store.public_watch(watch),
        "summary": describe(
            watch["criteria"], store.station_names(), store.country_names()
        ),
        "telegram_link": telegram.deep_link(watch["id"]),
        "telegram_enabled": telegram.enabled(),
    }


@app.post("/api/watches", status_code=201)
def create_watch(payload: WatchIn, request: Request):
    security.enforce_write_limit(request)
    if store.watch_count() >= security.MAX_TOTAL_WATCHES:
        raise HTTPException(503, "Saved-search capacity reached")
    try:
        watch = store.create_watch(payload.name, payload.criteria)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _watch_response(watch)


@app.get("/api/watches/{watch_id}")
def get_watch(watch_id: str):
    watch = store.get_watch(watch_id)
    if not watch:
        raise HTTPException(404, "No such saved search")
    return _watch_response(watch)


@app.patch("/api/watches/{watch_id}")
def patch_watch(watch_id: str, request: Request, payload: dict = Body(...)):
    security.enforce_write_limit(request)
    if not store.get_watch(watch_id):
        raise HTTPException(404, "No such saved search")
    try:
        watch = store.update_watch(
            watch_id,
            name=payload.get("name"),
            criteria=payload.get("criteria"),
            active=payload.get("active"),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _watch_response(watch)


@app.delete("/api/watches/{watch_id}", status_code=204)
def remove_watch(watch_id: str, request: Request):
    security.enforce_write_limit(request)
    if not store.get_watch(watch_id):
        raise HTTPException(404, "No such saved search")
    store.delete_watch(watch_id)


@app.get("/api/watches/{watch_id}/preview")
def preview_watch(watch_id: str):
    """What this search would match right now - shown before you commit to it."""
    watch = store.get_watch(watch_id)
    if not watch:
        raise HTTPException(404, "No such saved search")
    from .matcher import matches

    return [o for o in store.list_offers() if matches(o, watch["criteria"])]


@app.post("/api/crawl")
def trigger_crawl(x_admin_token: str | None = Header(None)):
    """Manual re-crawl. Token-gated so a public UI cannot hammer Roadsurfer."""
    # Fails closed when ADMIN_TOKEN is unset; compared in constant time so the
    # token cannot be recovered a byte at a time.
    if not ADMIN_TOKEN or not secrets.compare_digest(x_admin_token or "", ADMIN_TOKEN):
        raise HTTPException(403, "Forbidden")
    summary = run_crawl_and_notify()
    summary.pop("new_keys", None)
    return summary


@app.get("/api/crawls")
def recent_crawls(limit: int = Query(20, le=100)):
    rows = db.get_conn().execute(
        "SELECT * FROM crawls ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [db.row_to_dict(r) for r in rows]

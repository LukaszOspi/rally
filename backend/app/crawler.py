"""Hourly crawl of the rally graph, with diffing against the previous run."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from . import db
from .roadsurfer import (
    EUROPE,
    RoadsurferClient,
    RoadsurferError,
    RouteNotBookable,
    compact_vehicles,
    haversine_km,
    parse_station,
)

log = logging.getLogger(__name__)

# Windows whose pickup has passed are not bookable, so they never enter the DB.
# Detail (vehicles + price) is refreshed at most this often per offer.
DETAIL_TTL_HOURS = 24
# Windows that came back unbookable are retried sooner - stock does reappear.
UNBOOKABLE_RECHECK_HOURS = 6
# Ceiling on detail lookups per crawl so a big change never bursts the API.
# Sized to cover a full refresh of every live offer in one run; with the TTL
# above, steady state is far lower than this.
MAX_DETAIL_PER_CRAWL = 130
ROUTE_WORKERS = 6


def _iso_date(value: str) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10] or None


def crawl(client: RoadsurferClient | None = None, europe_only: bool = True) -> dict:
    """Run one full crawl. Returns a summary including the new offer keys."""
    client = client or RoadsurferClient()
    started = time.time()
    started_at = db.utcnow()

    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO crawls(started_at) VALUES (?)", (started_at,)
        )
        crawl_id = cur.lastrowid

    try:
        summary = _run(client, europe_only, started_at)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        log.exception("crawl failed")
        with db.tx() as conn:
            conn.execute(
                "UPDATE crawls SET finished_at=?, ok=0, error=?, duration_s=? "
                "WHERE id=?",
                (db.utcnow(), str(exc)[:500], round(time.time() - started, 2), crawl_id),
            )
        raise

    with db.tx() as conn:
        conn.execute(
            "UPDATE crawls SET finished_at=?, ok=1, n_stations=?, n_routes=?, "
            "n_offers=?, n_new=?, n_gone=?, n_detail_fetched=?, duration_s=? "
            "WHERE id=?",
            (
                db.utcnow(),
                summary["n_stations"],
                summary["n_routes"],
                summary["n_offers"],
                len(summary["new_keys"]),
                summary["n_gone"],
                summary["n_detail_fetched"],
                round(time.time() - started, 2),
                crawl_id,
            ),
        )
    db.set_meta("last_crawl_at", db.utcnow())
    summary["crawl_id"] = crawl_id
    summary["duration_s"] = round(time.time() - started, 2)
    log.info(
        "crawl done: %d stations, %d routes, %d offers (%d new, %d gone) in %.1fs",
        summary["n_stations"], summary["n_routes"], summary["n_offers"],
        len(summary["new_keys"]), summary["n_gone"], summary["duration_s"],
    )
    return summary


def _run(client: RoadsurferClient, europe_only: bool, seen_at: str) -> dict:
    raw_stations = client.stations()
    stations = [parse_station(s) for s in raw_stations]
    if europe_only:
        stations = [s for s in stations if (s["country"] or "") in EUROPE]
    by_id = {s["id"]: s for s in stations}

    with db.tx() as conn:
        for s in stations:
            conn.execute(
                """INSERT INTO stations(id, name, city, country, country_name,
                       address, zip, timezone, google_link, latitude, longitude,
                       one_way, updated_at)
                   VALUES(:id, :name, :city, :country, :country_name, :address,
                          :zip, :timezone, :google_link, :latitude, :longitude,
                          :one_way, :updated_at)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, city=excluded.city,
                       country=excluded.country, country_name=excluded.country_name,
                       address=excluded.address, zip=excluded.zip,
                       timezone=excluded.timezone, google_link=excluded.google_link,
                       latitude=excluded.latitude, longitude=excluded.longitude,
                       one_way=excluded.one_way, updated_at=excluded.updated_at""",
                {**s, "updated_at": seen_at},
            )

    departures = [s for s in stations if s["one_way"]]

    def fetch_routes(station):
        try:
            return station["id"], client.routes(station["id"])
        except RoadsurferError:
            log.warning("routes failed for station %s", station["id"])
            return station["id"], []

    with ThreadPoolExecutor(ROUTE_WORKERS) as pool:
        route_map = dict(pool.map(fetch_routes, departures))

    pairs = [
        (start, end)
        for start, ends in route_map.items()
        for end in ends
        # A destination outside the kept region has no station row to join to.
        if end in by_id
    ]

    def fetch_timeframes(pair):
        try:
            return pair, client.timeframes(*pair)
        except RoadsurferError:
            log.warning("timeframes failed for %s-%s", *pair)
            return pair, []

    with ThreadPoolExecutor(ROUTE_WORKERS) as pool:
        timeframe_results = list(pool.map(fetch_timeframes, pairs))

    today = date.today().isoformat()
    live_keys: set[str] = set()
    candidates: list[dict] = []

    for (start_id, end_id), windows in timeframe_results:
        for window in windows or []:
            start_date = _iso_date(window.get("startDate"))
            end_date = _iso_date(window.get("endDate"))
            if not start_date or not end_date:
                continue
            # Only what can still be booked.
            if start_date < today:
                continue
            span = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
            if span <= 0:
                continue
            key = f"{start_id}-{end_id}-{start_date}-{end_date}"
            live_keys.add(key)
            a, b = by_id[start_id], by_id[end_id]
            candidates.append({
                "key": key,
                "start_station_id": start_id,
                "end_station_id": end_id,
                "start_date": start_date,
                "end_date": end_date,
                "window_days": span,
                "distance_km": haversine_km(
                    a["latitude"], a["longitude"], b["latitude"], b["longitude"]
                ),
            })

    conn = db.get_conn()
    existing = {
        row["key"]: row
        for row in conn.execute(
            "SELECT key, active, detail_fetched_at FROM offers"
        ).fetchall()
    }

    new_keys: list[str] = []
    with db.tx() as conn:
        for offer in candidates:
            prior = existing.get(offer["key"])
            if prior is None:
                new_keys.append(offer["key"])
                conn.execute(
                    """INSERT INTO offers(key, start_station_id, end_station_id,
                           start_date, end_date, window_days, distance_km,
                           first_seen, last_seen, active)
                       VALUES(:key, :start_station_id, :end_station_id, :start_date,
                              :end_date, :window_days, :distance_km, :seen, :seen, 1)""",
                    {**offer, "seen": seen_at},
                )
            else:
                # A window that vanished and came back counts as new again.
                if not prior["active"]:
                    new_keys.append(offer["key"])
                conn.execute(
                    "UPDATE offers SET last_seen=?, active=1, gone_at=NULL, "
                    "distance_km=?, window_days=? WHERE key=?",
                    (seen_at, offer["distance_km"], offer["window_days"], offer["key"]),
                )

        # Anything previously active but absent this run is gone.
        placeholders = ",".join("?" * len(live_keys)) if live_keys else "''"
        gone = conn.execute(
            f"SELECT key FROM offers WHERE active=1 AND key NOT IN ({placeholders})",
            tuple(live_keys),
        ).fetchall()
        for row in gone:
            conn.execute(
                "UPDATE offers SET active=0, gone_at=? WHERE key=?",
                (seen_at, row["key"]),
            )

    n_detail = _refresh_details(client, new_keys)

    # Only genuinely bookable arrivals are worth alerting on. Anything still
    # unresolved stays out of this round and is picked up once it is known.
    if new_keys:
        marks = ",".join("?" * len(new_keys))
        rows = db.get_conn().execute(
            f"SELECT key FROM offers WHERE bookable = 1 AND key IN ({marks})",
            tuple(new_keys),
        ).fetchall()
        notifiable = [r["key"] for r in rows]
    else:
        notifiable = []

    bookable_total = db.get_conn().execute(
        "SELECT COUNT(*) n FROM offers WHERE active = 1 AND bookable = 1"
    ).fetchone()["n"]

    return {
        "n_stations": len(stations),
        "n_routes": len(pairs),
        "n_offers": len(candidates),
        "n_bookable": bookable_total,
        "new_keys": notifiable,
        "n_new_candidates": len(new_keys),
        "n_gone": len(gone),
        "n_detail_fetched": n_detail,
    }


def _refresh_details(client: RoadsurferClient, new_keys: list[str]) -> int:
    """Resolve vehicles, price and real bookability for offers that need it.

    A window returned by `timeframes` is only a candidate. Whether anything can
    actually be booked on it is knowable solely by running the search, so this
    step is what separates a real trip from a dead end.
    """
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=DETAIL_TTL_HOURS)).isoformat(timespec="seconds")
    recheck = (now - timedelta(hours=UNBOOKABLE_RECHECK_HOURS)).isoformat(
        timespec="seconds"
    )

    rows = db.get_conn().execute(
        """SELECT key, start_station_id, end_station_id, start_date, end_date
           FROM offers
           WHERE active = 1
             AND (
                   detail_fetched_at IS NULL
                OR (bookable = 1 AND detail_fetched_at < ?)
                OR (COALESCE(bookable, 0) = 0 AND detail_fetched_at < ?)
             )
           ORDER BY (detail_fetched_at IS NULL) DESC, start_date ASC
           LIMIT ?""",
        (fresh, recheck, MAX_DETAIL_PER_CRAWL),
    ).fetchall()

    # New offers first: they drive alerts and must carry price and bookability
    # by the time the notifier runs.
    new_set = set(new_keys)
    ordered = sorted(rows, key=lambda r: r["key"] not in new_set)

    fetched = 0
    for row in ordered:
        try:
            result = client.search(
                row["start_station_id"], row["end_station_id"],
                row["start_date"], row["end_date"],
            )
            detail = compact_vehicles(result)
        except RouteNotBookable as exc:
            log.info("not bookable: %s (%s)", row["key"], exc)
            detail = {"vehicles": [], "pickup": None, "dropoff": None,
                      "price": None, "currency": "EUR", "reason": str(exc)}
        except RoadsurferError:
            log.warning("search failed for %s", row["key"])
            continue

        bookable = 1 if any(v.get("available") for v in detail["vehicles"]) else 0
        if not bookable and not detail.get("reason"):
            # Booking accepted the route but had nothing free on these dates.
            detail["reason"] = (
                "all_vehicles_taken" if detail["vehicles"] else "no_vehicles_offered"
            )
        with db.tx() as conn:
            conn.execute(
                "UPDATE offers SET detail_json=?, detail_fetched_at=?, price=?, "
                "currency=?, bookable=? WHERE key=?",
                (
                    json.dumps(detail, separators=(",", ":")),
                    db.utcnow(),
                    detail.get("price"),
                    detail.get("currency"),
                    bookable,
                    row["key"],
                ),
            )
        fetched += 1
    return fetched


def purge_expired() -> int:
    """Deactivate offers whose pickup date has passed since the last crawl."""
    today = date.today().isoformat()
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE offers SET active=0, gone_at=? WHERE active=1 AND start_date < ?",
            (db.utcnow(), today),
        )
    return cur.rowcount

"""Query helpers shared by the HTTP API and the alert matcher."""

import json
import secrets
from datetime import date, datetime, timedelta, timezone

from . import db
from .matcher import normalise_criteria
from .security import MAX_CRITERIA_BYTES

OFFER_SELECT = """
SELECT o.key, o.start_station_id, o.end_station_id, o.start_date, o.end_date,
       o.window_days, o.distance_km, o.first_seen, o.last_seen, o.active,
       o.price, o.currency, o.detail_json, o.detail_fetched_at, o.bookable,
       s.name AS start_name, s.city AS start_city, s.country AS start_country,
       s.country_name AS start_country_name, s.latitude AS start_lat,
       s.longitude AS start_lon, s.address AS start_address,
       s.google_link AS start_google,
       e.name AS end_name, e.city AS end_city, e.country AS end_country,
       e.country_name AS end_country_name, e.latitude AS end_lat,
       e.longitude AS end_lon, e.address AS end_address,
       e.google_link AS end_google
FROM offers o
JOIN stations s ON s.id = o.start_station_id
JOIN stations e ON e.id = o.end_station_id
"""


def _shape(row) -> dict:
    d = db.row_to_dict(row)
    detail = db.loads(d.pop("detail_json"), {}) or {}
    d["detail"] = detail
    d["vehicles"] = detail.get("vehicles") or []
    d["pickup"] = detail.get("pickup")
    d["dropoff"] = detail.get("dropoff")
    d["unbookable_reason"] = detail.get("reason") if d.get("bookable") == 0 else None
    d["active"] = bool(d["active"])
    return d


def list_offers(
    active_only: bool = True,
    start_station_ids: list[int] | None = None,
    end_station_ids: list[int] | None = None,
    countries: list[str] | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    min_days: int | None = None,
    max_days: int | None = None,
    new_since: str | None = None,
    include_unbookable: bool = False,
    limit: int = 1000,
) -> list[dict]:
    where, params = [], []
    if active_only:
        where.append("o.active = 1")
        # Defensive: a pickup date in the past is never bookable even if the
        # last crawl still listed it.
        where.append("o.start_date >= ?")
        params.append(date.today().isoformat())
    if not include_unbookable:
        # NULL means "not checked yet" and is kept - the UI labels it as such.
        # Only a confirmed 0 is hidden.
        where.append("(o.bookable IS NULL OR o.bookable = 1)")
    if start_station_ids:
        where.append(f"o.start_station_id IN ({','.join('?' * len(start_station_ids))})")
        params += start_station_ids
    if end_station_ids:
        where.append(f"o.end_station_id IN ({','.join('?' * len(end_station_ids))})")
        params += end_station_ids
    if countries:
        marks = ",".join("?" * len(countries))
        where.append(f"(s.country IN ({marks}) OR e.country IN ({marks}))")
        params += countries + countries
    if earliest:
        where.append("o.start_date >= ?")
        params.append(earliest)
    if latest:
        where.append("o.start_date <= ?")
        params.append(latest)
    if min_days is not None:
        where.append("o.window_days >= ?")
        params.append(min_days)
    if max_days is not None:
        where.append("o.window_days <= ?")
        params.append(max_days)
    if new_since:
        where.append("o.first_seen >= ?")
        params.append(new_since)

    sql = OFFER_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.start_date ASC, s.name ASC LIMIT ?"
    params.append(limit)

    return [_shape(r) for r in db.get_conn().execute(sql, params).fetchall()]


def offers_by_keys(keys: list[str]) -> list[dict]:
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    sql = f"{OFFER_SELECT} WHERE o.key IN ({marks})"
    return [_shape(r) for r in db.get_conn().execute(sql, keys).fetchall()]


def stations(one_way_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM stations"
    if one_way_only:
        sql += " WHERE one_way = 1"
    sql += " ORDER BY name"
    rows = db.get_conn().execute(sql).fetchall()
    return [db.row_to_dict(r) for r in rows]


def station_names() -> dict[int, str]:
    rows = db.get_conn().execute("SELECT id, name FROM stations").fetchall()
    return {r["id"]: r["name"] for r in rows}


def country_names() -> dict[str, str]:
    rows = db.get_conn().execute(
        "SELECT DISTINCT country, country_name FROM stations "
        "WHERE country IS NOT NULL"
    ).fetchall()
    return {r["country"]: r["country_name"] or r["country"] for r in rows}


def models() -> list[dict]:
    """Every camper model ever seen, with how many trips currently offer it.

    Built from all offers, not just live ones, so a model that is temporarily
    absent can still be picked for an alert.
    """
    today = date.today().isoformat()
    rows = db.get_conn().execute(
        "SELECT detail_json, active, bookable, start_date FROM offers "
        "WHERE detail_json IS NOT NULL"
    ).fetchall()

    found: dict[str, dict] = {}
    for row in rows:
        detail = db.loads(row["detail_json"], {}) or {}
        live = (
            row["active"]
            and row["bookable"] == 1
            and (row["start_date"] or "") >= today
        )
        for v in detail.get("vehicles") or []:
            name = (v.get("name") or "").strip()
            if not name:
                continue
            entry = found.setdefault(name, {
                "name": name,
                "camper_type": v.get("camper_type"),
                "seats": v.get("seats"),
                "beds": v.get("beds"),
                "available_now": 0,
            })
            if entry["seats"] is None:
                entry["seats"] = v.get("seats")
            if entry["beds"] is None:
                entry["beds"] = v.get("beds")
            if live and v.get("available"):
                entry["available_now"] += 1

    return sorted(
        found.values(),
        key=lambda m: (-m["available_now"], m["name"]),
    )


def stats() -> dict:
    conn = db.get_conn()
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT start_station_id) starts, "
        "COUNT(DISTINCT end_station_id) ends, MIN(start_date) first, "
        "MAX(start_date) last FROM offers WHERE active=1 AND start_date >= ? "
        "AND (bookable IS NULL OR bookable = 1)",
        (today,),
    ).fetchone()
    unbookable = conn.execute(
        "SELECT COUNT(*) n FROM offers WHERE active=1 AND start_date >= ? "
        "AND bookable = 0",
        (today,),
    ).fetchone()["n"]
    last_crawl = conn.execute(
        "SELECT * FROM crawls WHERE ok=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    new_count = conn.execute(
        "SELECT COUNT(*) n FROM offers WHERE active=1 AND first_seen >= ? "
        "AND start_date >= ? AND (bookable IS NULL OR bookable = 1)",
        (day_ago, today),
    ).fetchone()["n"]
    return {
        "offers": row["n"],
        "departure_stations": row["starts"],
        "destination_stations": row["ends"],
        "earliest_departure": row["first"],
        "latest_departure": row["last"],
        "new_last_24h": new_count,
        "filtered_unbookable": unbookable,
        "last_crawl": db.row_to_dict(last_crawl) if last_crawl else None,
    }


# ── saved searches ───────────────────────────────────────────────────────────

def watch_count() -> int:
    return db.get_conn().execute("SELECT COUNT(*) n FROM watches").fetchone()["n"]


def create_watch(name: str, criteria: dict) -> dict:
    # 32 hex chars: unguessable, so a public UI can still keep searches private.
    watch_id = secrets.token_hex(16)
    clean = normalise_criteria(criteria)
    payload = json.dumps(clean)
    if len(payload) > MAX_CRITERIA_BYTES:
        raise ValueError("criteria too large")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO watches(id, name, criteria_json, created_at, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (watch_id, name[:120] or "Untitled", payload, db.utcnow()),
        )
    return get_watch(watch_id)


def get_watch(watch_id: str) -> dict | None:
    row = db.get_conn().execute(
        "SELECT * FROM watches WHERE id = ?", (watch_id,)
    ).fetchone()
    if not row:
        return None
    d = db.row_to_dict(row)
    d["criteria"] = db.loads(d.pop("criteria_json"), {})
    d["active"] = bool(d["active"])
    d["linked"] = bool(d.get("chat_id"))
    return d


def public_watch(watch: dict | None) -> dict | None:
    """Same record minus the Telegram chat id.

    Anyone holding the search link can GET it, and links get shared and
    screenshotted. `linked` already says whether a chat is attached, so the
    id itself has no reason to leave the server.
    """
    if not watch:
        return None
    return {k: v for k, v in watch.items() if k != "chat_id"}


def update_watch(watch_id: str, name=None, criteria=None, active=None) -> dict | None:
    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(name[:120])
    if criteria is not None:
        payload = json.dumps(normalise_criteria(criteria))
        if len(payload) > MAX_CRITERIA_BYTES:
            raise ValueError("criteria too large")
        sets.append("criteria_json = ?")
        params.append(payload)
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if sets:
        params.append(watch_id)
        with db.tx() as conn:
            conn.execute(f"UPDATE watches SET {', '.join(sets)} WHERE id = ?", params)
    return get_watch(watch_id)


def delete_watch(watch_id: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM watch_hits WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))


def link_watch(watch_id: str, chat_id: str) -> dict | None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE watches SET chat_id = ?, linked_at = ? WHERE id = ?",
            (str(chat_id), db.utcnow(), watch_id),
        )
    return get_watch(watch_id)


def watches_for_chat(chat_id: str) -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT id FROM watches WHERE chat_id = ? ORDER BY created_at", (str(chat_id),)
    ).fetchall()
    return [get_watch(r["id"]) for r in rows]


def active_linked_watches() -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT id FROM watches WHERE active = 1 AND chat_id IS NOT NULL"
    ).fetchall()
    return [get_watch(r["id"]) for r in rows]


def already_notified(watch_id: str, offer_key: str) -> bool:
    row = db.get_conn().execute(
        "SELECT 1 FROM watch_hits WHERE watch_id = ? AND offer_key = ?",
        (watch_id, offer_key),
    ).fetchone()
    return row is not None


def record_hit(watch_id: str, offer_key: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watch_hits(watch_id, offer_key, notified_at) "
            "VALUES (?, ?, ?)",
            (watch_id, offer_key, db.utcnow()),
        )
        conn.execute(
            "UPDATE watches SET last_notified_at = ? WHERE id = ?",
            (db.utcnow(), watch_id),
        )

"""Saved-search ("Suchanfrage") criteria matching.

Every criterion is optional; an absent or empty one means "any". A saved search
with no criteria at all therefore matches every new offer, which is a legitimate
"tell me about anything" watch.
"""

from typing import Any

from .security import MAX_CODES_PER_FIELD, MAX_IDS_PER_FIELD

CRITERIA_FIELDS = (
    "start_station_ids", "end_station_ids", "start_countries", "end_countries",
    "earliest_date", "latest_date", "min_days", "max_days", "max_price",
    "min_seats", "models",
)


def normalise_criteria(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    out: dict[str, Any] = {}

    for field in ("start_station_ids", "end_station_ids"):
        values = raw.get(field) or []
        if not isinstance(values, (list, tuple)):
            continue
        ids = []
        # Bounded before parsing: this is written straight to the DB from an
        # unauthenticated endpoint, so an unbounded list is a storage attack.
        for v in values[:MAX_IDS_PER_FIELD]:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if ids:
            out[field] = sorted(set(ids))

    for field in ("start_countries", "end_countries"):
        values = raw.get(field) or []
        if not isinstance(values, (list, tuple)):
            continue
        codes = sorted({str(v).upper()[:2] for v in values[:MAX_CODES_PER_FIELD] if v})
        if codes:
            out[field] = codes

    raw_models = raw.get("models") or []
    if isinstance(raw_models, (list, tuple)):
        models = [
            str(m).strip().upper()[:60]
            for m in raw_models[:MAX_CODES_PER_FIELD]
            if str(m).strip()
        ]
        if models:
            out["models"] = sorted(set(models))

    for field in ("earliest_date", "latest_date"):
        value = raw.get(field)
        if value:
            out[field] = str(value)[:10]

    for field in ("min_days", "max_days", "min_seats"):
        value = raw.get(field)
        if value not in (None, ""):
            try:
                out[field] = int(value)
            except (TypeError, ValueError):
                pass

    if raw.get("max_price") not in (None, ""):
        try:
            out["max_price"] = float(raw["max_price"])
        except (TypeError, ValueError):
            pass

    return out


def matches(offer: dict, criteria: dict[str, Any]) -> bool:
    """`offer` is an enriched row: station fields resolved, detail parsed."""
    if not criteria:
        return True

    ids = criteria.get("start_station_ids")
    if ids and offer["start_station_id"] not in ids:
        return False

    ids = criteria.get("end_station_ids")
    if ids and offer["end_station_id"] not in ids:
        return False

    codes = criteria.get("start_countries")
    if codes and (offer.get("start_country") or "").upper() not in codes:
        return False

    codes = criteria.get("end_countries")
    if codes and (offer.get("end_country") or "").upper() not in codes:
        return False

    earliest = criteria.get("earliest_date")
    if earliest and offer["start_date"] < earliest:
        return False

    latest = criteria.get("latest_date")
    # The trip must be able to start on or before the latest acceptable date.
    if latest and offer["start_date"] > latest:
        return False

    days = offer.get("window_days") or 0
    if criteria.get("min_days") is not None and days < criteria["min_days"]:
        return False
    if criteria.get("max_days") is not None and days > criteria["max_days"]:
        return False

    max_price = criteria.get("max_price")
    if max_price is not None:
        price = offer.get("price")
        # An offer with no price yet has not been detail-fetched; do not
        # silently drop it from a price-capped watch.
        if price is not None and price > max_price:
            return False

    vehicles = (offer.get("detail") or {}).get("vehicles") or []

    wanted = criteria.get("models")
    if wanted:
        # Only campers you could actually take count towards a model match.
        names = {
            (v.get("name") or "").upper() for v in vehicles if v.get("available")
        }
        if names and not (names & set(wanted)):
            return False

    min_seats = criteria.get("min_seats")
    if min_seats is not None and vehicles:
        seats = [v.get("seats") for v in vehicles if v.get("seats")]
        if seats and max(seats) < min_seats:
            return False

    return True


def describe(
    criteria: dict[str, Any],
    station_names: dict[int, str],
    country_names: dict[str, str] | None = None,
) -> str:
    """Short human summary of a watch, for Telegram and the UI."""
    if not criteria:
        return "anything new"

    country_names = country_names or {}
    parts = []

    def names(ids):
        return ", ".join(station_names.get(i, str(i)) for i in ids)

    def countries(codes):
        return ", ".join(country_names.get(c, c) for c in codes)

    if criteria.get("start_station_ids"):
        parts.append(f"from {names(criteria['start_station_ids'])}")
    elif criteria.get("start_countries"):
        parts.append(f"from {countries(criteria['start_countries'])}")

    if criteria.get("end_station_ids"):
        parts.append(f"to {names(criteria['end_station_ids'])}")
    elif criteria.get("end_countries"):
        parts.append(f"to {countries(criteria['end_countries'])}")

    earliest, latest = criteria.get("earliest_date"), criteria.get("latest_date")
    if earliest and latest:
        parts.append(f"departing {earliest} to {latest}")
    elif earliest:
        parts.append(f"departing from {earliest}")
    elif latest:
        parts.append(f"departing by {latest}")

    lo, hi = criteria.get("min_days"), criteria.get("max_days")
    if lo and hi:
        parts.append(f"{lo}-{hi} days")
    elif lo:
        parts.append(f"at least {lo} days")
    elif hi:
        parts.append(f"up to {hi} days")

    if criteria.get("max_price") is not None:
        parts.append(f"under {criteria['max_price']:.0f} EUR")
    if criteria.get("min_seats"):
        parts.append(f"{criteria['min_seats']}+ seats")
    if criteria.get("models"):
        parts.append(", ".join(criteria["models"]).title())

    return " · ".join(parts) if parts else "anything new"

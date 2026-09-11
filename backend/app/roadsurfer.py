"""Client for the Roadsurfer Rally endpoints.

The booking frontend is a Nuxt app talking to `/api/{locale}/rally/*`. Every
call carries an `X-Requested-Alias` header naming the logical endpoint; the
gateway returns the SPA's 404 page without it, so it is required, not cosmetic.
"""

import json
import logging
import math
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE = "https://booking.roadsurfer.com/api"
LOCALE = "en"

# Roadsurfer serves rally stations well beyond Europe; these are the ones we
# keep by default. The API itself has no region field, only a country code.
EUROPE = {
    "AT", "BE", "CH", "CZ", "DE", "DK", "ES", "FI", "FR", "GB", "HR", "HU",
    "IE", "IT", "LU", "NL", "NO", "PL", "PT", "SE", "SI", "SK",
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class RoadsurferError(RuntimeError):
    pass


class RouteNotBookable(RoadsurferError):
    """The timeframes endpoint offered this window but booking rejects it.

    Roadsurfer lists windows per route that the booking funnel then refuses
    with `invalid_booking_end_station`. Treated as "not bookable", not an error.
    """


class RoadsurferClient:
    def __init__(self, locale: str = LOCALE, timeout: int = 30, delay: float = 0.15):
        self.locale = locale
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en;q=0.9",
            "User-Agent": USER_AGENT,
            "Referer": "https://booking.roadsurfer.com/rally/",
            "Origin": "https://booking.roadsurfer.com",
        })

    def _get(self, path: str, alias: str, params: dict | None = None) -> Any:
        url = f"{BASE}/{self.locale}/{path}"
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers={"X-Requested-Alias": alias},
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    time.sleep(self.delay)
                    return resp.json()
                # 5xx and 429 are worth retrying; a 404 here means the alias or
                # the path is wrong and retrying will not help.
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = RoadsurferError(f"{resp.status_code} for {url}")
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code == 400:
                    kind = ""
                    try:
                        kind = (resp.json() or {}).get("type", "")
                    except ValueError:
                        pass
                    if kind.startswith("invalid_booking"):
                        raise RouteNotBookable(kind)
                raise RoadsurferError(
                    f"HTTP {resp.status_code} for {url} (alias={alias})"
                )
            except (requests.RequestException, json.JSONDecodeError) as exc:
                last_err = exc
                time.sleep(2 ** attempt)
        raise RoadsurferError(f"failed after retries: {url}") from last_err

    def stations(self) -> list[dict]:
        """Every station. Those with one_way=True are the rally departure points."""
        return self._get("rally/stations", "rally.startStations")

    def routes(self, station_id: int) -> list[int]:
        """Destination station ids reachable from a departure station."""
        data = self._get(f"rally/stations/{station_id}", "rally.fetchRoutes")
        return data.get("returns") or []

    def timeframes(self, start_id: int, end_id: int) -> list[dict]:
        """Bookable date windows for a route, as {startDate, endDate}."""
        return self._get(
            f"rally/timeframes/{start_id}-{end_id}", "rally.timeframes"
        )

    def search(
        self, start_id: int, end_id: int, pickup: str, dropoff: str,
        currency: str = "EUR",
    ) -> list[dict]:
        """Vehicles bookable for a concrete route and date range."""
        return self._get(
            "rally/search",
            "rally.search",
            params={
                "stations": json.dumps([[start_id, end_id]]),
                "range": json.dumps([pickup, dropoff]),
                "currency": currency,
            },
        )


def haversine_km(lat1, lon1, lat2, lon2) -> float | None:
    """Great-circle distance. Straight line, not driving distance."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def parse_station(raw: dict) -> dict:
    city = raw.get("city") or {}
    coord = raw.get("coordinate") or {}
    return {
        "id": raw["id"],
        "name": raw.get("name") or city.get("name") or f"Station {raw['id']}",
        "city": city.get("name"),
        "country": city.get("country"),
        "country_name": city.get("country_name"),
        "address": raw.get("address"),
        "zip": raw.get("zip"),
        "timezone": raw.get("timezone"),
        "google_link": raw.get("google_link"),
        "latitude": coord.get("latitude"),
        "longitude": coord.get("longitude"),
        "one_way": 1 if raw.get("one_way") else 0,
    }


def _clean_name(name: str | None) -> str:
    """Normalise model names to one casing.

    Roadsurfer returns the same camper as "SURFER SUITE" or "Surfer Suite"
    depending on whether the category or the model name wins, and sometimes
    both appear in one response. Title case matches the product branding.
    """
    if not name:
        return "Camper"
    name = name.strip()
    return name.title() if name.isupper() else name


def compact_vehicles(search_result: list[dict]) -> dict:
    """Reduce a search response to what the UI actually shows.

    The raw payload repeats the same image list many times over and carries
    insurance and tax breakdowns we do not need, so ~28 KB collapses to ~1 KB.
    """
    by_model: dict = {}
    pickup = dropoff = None
    price = currency = None

    for item in search_result or []:
        avail = item.get("availability") or {}
        # Roadsurfer pads the response with placeholder entries (id like
        # "3-78-empty") carrying no availability. They mean "nothing bookable
        # here", so they must not become vehicles.
        if not avail:
            continue
        model = item.get("model") or {}
        pricing = avail.get("price_details") or {}

        images, seen = [], set()
        for entry in (model.get("images") or []):
            url = ((entry or {}).get("image") or {}).get("url")
            if url and url not in seen:
                seen.add(url)
                images.append(url)

        final = pricing.get("final") or pricing.get("gross")
        name = _clean_name(model.get("name") or item.get("name"))
        # The same camper arrives under different casing, and Roadsurfer also
        # ships several model ids for one product (Surfer Suite 1 and 2, same
        # spec and price, different photos). Merge on what a traveller can tell
        # apart: the name and the sleeping/seating spec.
        key = (name.lower(), model.get("seats"), model.get("beds"))
        entry = by_model.get(key)
        if entry is None:
            by_model[key] = {
                "id": model.get("id") or item.get("id"),
                "name": name,
                "camper_type": model.get("camper_type"),
                "seats": model.get("seats"),
                "beds": model.get("beds"),
                "available": bool(item.get("available", True)),
                "price": final,
                "images": images[:6],
            }
        else:
            entry["available"] = entry["available"] or bool(item.get("available", True))
            if entry["price"] is None:
                entry["price"] = final
            for url in images:
                if len(entry["images"]) >= 6:
                    break
                if url not in entry["images"]:
                    entry["images"].append(url)

        if price is None and final is not None:
            price = final
        if currency is None:
            currency = pricing.get("currency") or "EUR"

        if pickup is None and avail.get("pickup_date"):
            ps = avail.get("pickup_station") or {}
            rs = avail.get("return_station") or {}
            pickup = {
                "date": avail.get("pickup_date"),
                "from": avail.get("pickup_from"),
                "to": avail.get("pickup_to"),
                "station_id": ps.get("id"),
                "address": ps.get("address"),
            }
            dropoff = {
                "date": avail.get("return_date"),
                "from": avail.get("return_from"),
                "to": avail.get("return_to"),
                "station_id": rs.get("id"),
                "address": rs.get("address"),
            }

    vehicles = sorted(
        by_model.values(), key=lambda v: (not v["available"], v.get("name") or "")
    )
    return {
        "vehicles": vehicles,
        "pickup": pickup,
        "dropoff": dropoff,
        "price": price,
        "currency": currency or "EUR",
    }

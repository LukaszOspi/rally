"""Unit tests for the parts with real decision logic: matching and compaction."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RALLY_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from app.matcher import describe, matches, normalise_criteria  # noqa: E402
from app.roadsurfer import compact_vehicles, haversine_km  # noqa: E402


def offer(**kw):
    base = {
        "key": "1-2-2026-09-01-2026-09-08",
        "start_station_id": 1,
        "end_station_id": 2,
        "start_country": "FR",
        "end_country": "DE",
        "start_date": "2026-09-01",
        "end_date": "2026-09-08",
        "window_days": 7,
        "price": 129.01,
        "detail": {"vehicles": [{"name": "Surfer Suite", "seats": 4, "available": True}]},
    }
    base.update(kw)
    return base


# ── criteria matching ────────────────────────────────────────────────────────

def test_empty_criteria_matches_everything():
    assert matches(offer(), {})


def test_station_and_country_filters():
    assert matches(offer(), {"start_station_ids": [1]})
    assert not matches(offer(), {"start_station_ids": [99]})
    assert matches(offer(), {"start_countries": ["FR"]})
    assert not matches(offer(), {"start_countries": ["ES"]})
    assert matches(offer(), {"end_countries": ["DE"]})


def test_date_window_bounds_are_on_departure():
    assert matches(offer(), {"earliest_date": "2026-08-01"})
    assert not matches(offer(), {"earliest_date": "2026-09-02"})
    assert matches(offer(), {"latest_date": "2026-09-01"})
    assert not matches(offer(), {"latest_date": "2026-08-31"})


def test_duration_bounds():
    assert matches(offer(), {"min_days": 7, "max_days": 7})
    assert not matches(offer(), {"min_days": 8})
    assert not matches(offer(), {"max_days": 6})


def test_price_cap_ignores_offers_with_no_price_yet():
    # An offer whose detail has not been fetched must not be silently dropped.
    assert matches(offer(price=None), {"max_price": 50})
    assert not matches(offer(price=200.0), {"max_price": 150})
    assert matches(offer(price=100.0), {"max_price": 150})


def test_seat_and_model_filters():
    assert matches(offer(), {"min_seats": 4})
    assert not matches(offer(), {"min_seats": 6})
    assert matches(offer(), {"models": ["SURFER SUITE"]})
    assert not matches(offer(), {"models": ["ROAD HOUSE"]})


def test_seats_use_the_roomiest_vehicle():
    o = offer(detail={"vehicles": [
        {"name": "A", "seats": 2, "available": True},
        {"name": "B", "seats": 5, "available": True},
    ]})
    assert matches(o, {"min_seats": 5})


# ── criteria normalisation ───────────────────────────────────────────────────

def test_normalise_drops_empties_and_coerces_types():
    c = normalise_criteria({
        "start_station_ids": ["3", 4, "bad"],
        "start_countries": ["fr", "de"],
        "models": [" surfer suite "],
        "min_days": "5",
        "max_price": "150.5",
        "end_station_ids": [],
        "earliest_date": "2026-09-01T00:00:00",
    })
    assert c["start_station_ids"] == [3, 4]
    assert c["start_countries"] == ["DE", "FR"]
    assert c["models"] == ["SURFER SUITE"]
    assert c["min_days"] == 5
    assert c["max_price"] == 150.5
    assert c["earliest_date"] == "2026-09-01"
    assert "end_station_ids" not in c


def test_describe_uses_names_not_codes():
    text = describe(
        {"start_countries": ["ES"], "min_days": 5},
        {}, {"ES": "Spain"},
    )
    assert "Spain" in text and "at least 5 days" in text
    assert describe({}, {}) == "anything new"


# ── search response compaction ───────────────────────────────────────────────

def test_placeholder_entries_are_not_vehicles():
    """Roadsurfer pads responses with availability-less rows meaning 'nothing'."""
    out = compact_vehicles([{"id": "3-78-empty", "name": None}])
    assert out["vehicles"] == []
    assert out["price"] is None


def test_same_model_in_two_casings_is_merged():
    payload = [
        {
            "id": 1, "name": "SURFER SUITE", "available": False,
            "model": {"id": 1, "name": "SURFER SUITE", "seats": 4, "beds": 4,
                      "images": [{"image": {"url": "a.jpg"}}]},
            "availability": {"pickup_date": "2026-09-01", "pickup_from": "09:00:00",
                             "pickup_to": "18:00:00", "return_date": "2026-09-08",
                             "price_details": {"final": 129.01},
                             "pickup_station": {"id": 1, "address": "A st"},
                             "return_station": {"id": 2, "address": "B st"}},
        },
        {
            "id": 1, "name": "Surfer Suite", "available": True,
            "model": {"id": 1, "name": "Surfer Suite", "seats": 4, "beds": 4,
                      "images": []},
            "availability": {"price_details": {"final": 129.01}},
        },
    ]
    out = compact_vehicles(payload)
    assert len(out["vehicles"]) == 1
    v = out["vehicles"][0]
    assert v["name"] == "Surfer Suite"      # title case wins
    assert v["available"] is True            # availability is OR-ed
    assert v["images"] == ["a.jpg"]
    assert out["pickup"]["address"] == "A st"
    assert out["dropoff"]["address"] == "B st"


def test_distinct_model_ids_with_one_product_name_merge():
    """Roadsurfer ships Surfer Suite as ids 1 and 2 with identical specs."""
    payload = [
        {"id": 1, "name": "Surfer Suite", "available": True,
         "model": {"id": 1, "name": "Surfer Suite", "seats": 4, "beds": 4,
                   "images": [{"image": {"url": "a.jpg"}}]},
         "availability": {"price_details": {"final": 129.01}}},
        {"id": 2, "name": "Surfer Suite", "available": True,
         "model": {"id": 2, "name": "Surfer Suite", "seats": 4, "beds": 4,
                   "images": [{"image": {"url": "b.jpg"}}]},
         "availability": {"price_details": {"final": 129.01}}},
    ]
    out = compact_vehicles(payload)
    assert len(out["vehicles"]) == 1
    assert out["vehicles"][0]["images"] == ["a.jpg", "b.jpg"]


def test_different_specs_stay_separate():
    payload = [
        {"id": 1, "name": "Road House", "available": True,
         "model": {"id": 1, "name": "Road House", "seats": 4, "beds": 4},
         "availability": {"price_details": {"final": 129.0}}},
        {"id": 9, "name": "Cozy Cottage", "available": True,
         "model": {"id": 9, "name": "Cozy Cottage", "seats": 3, "beds": 3},
         "availability": {"price_details": {"final": 129.0}}},
    ]
    assert len(compact_vehicles(payload)["vehicles"]) == 2


def test_uppercase_only_name_is_title_cased():
    out = compact_vehicles([{
        "id": 5, "name": "ROAD HOUSE", "available": True,
        "model": {"id": 5, "name": "ROAD HOUSE"},
        "availability": {"price_details": {"final": 129.0}},
    }])
    assert out["vehicles"][0]["name"] == "Road House"


def test_haversine_is_sane():
    # Munich to Hamburg, roughly 600 km straight line.
    d = haversine_km(48.1351, 11.5820, 53.5511, 9.9937)
    assert 590 < d < 640
    assert haversine_km(None, 1, 2, 3) is None

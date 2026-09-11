"""Abuse limits on the public, unauthenticated surface."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RALLY_DB"] = os.path.join(tempfile.mkdtemp(), "sec.db")

import pytest  # noqa: E402

from app import db, store  # noqa: E402
from app.matcher import normalise_criteria  # noqa: E402
from app.security import (  # noqa: E402
    MAX_CRITERIA_BYTES,
    MAX_IDS_PER_FIELD,
    RateLimiter,
)


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sec.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init_db()
    yield
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


# ── rate limiting ────────────────────────────────────────────────────────────

def test_burst_is_capped_per_key():
    limiter = RateLimiter(limits=((3, 60),))
    assert [limiter.check("1.2.3.4")[0] for _ in range(5)] == [
        True, True, True, False, False
    ]


def test_limits_are_per_ip_not_global():
    limiter = RateLimiter(limits=((2, 60),))
    assert limiter.check("a")[0] and limiter.check("a")[0]
    assert limiter.check("a")[0] is False
    # A different client is unaffected.
    assert limiter.check("b")[0] is True


def test_rejected_request_reports_retry_after():
    limiter = RateLimiter(limits=((1, 60),))
    limiter.check("x")
    ok, retry_after = limiter.check("x")
    assert ok is False and 0 < retry_after <= 61


def test_idle_keys_are_garbage_collected():
    """A spray of unique IPs must not grow the table without bound."""
    limiter = RateLimiter(limits=((5, 1),))
    for i in range(200):
        limiter.check(f"10.0.0.{i}")
    limiter._gc(now=limiter.hits["10.0.0.0"][0] + 10_000, longest=1)
    assert limiter.hits == {}


# ── payload bounds ───────────────────────────────────────────────────────────

def test_oversized_id_lists_are_truncated_not_stored_whole():
    c = normalise_criteria({"start_station_ids": list(range(100_000))})
    assert len(c["start_station_ids"]) == MAX_IDS_PER_FIELD


def test_non_list_criteria_values_are_ignored_not_crashed():
    c = normalise_criteria({
        "start_station_ids": "not-a-list",
        "start_countries": {"nope": 1},
        "models": 42,
    })
    assert "start_station_ids" not in c
    assert "start_countries" not in c
    assert "models" not in c


def test_model_names_are_length_capped():
    c = normalise_criteria({"models": ["x" * 5_000]})
    assert len(c["models"][0]) <= 60


def test_criteria_that_survive_truncation_still_fit_the_column():
    huge = {
        "start_station_ids": list(range(1000)),
        "end_station_ids": list(range(1000)),
        "models": ["y" * 100] * 100,
    }
    watch = store.create_watch("big", huge)
    import json
    assert len(json.dumps(watch["criteria"])) <= MAX_CRITERIA_BYTES


# ── storage ceiling ──────────────────────────────────────────────────────────

def test_watch_count_tracks_inserts():
    assert store.watch_count() == 0
    store.create_watch("a", {})
    store.create_watch("b", {})
    assert store.watch_count() == 2


# ── chat id redaction ────────────────────────────────────────────────────────

def test_public_watch_strips_the_telegram_chat_id():
    watch = store.create_watch("mine", {})
    store.link_watch(watch["id"], "555123")
    full = store.get_watch(watch["id"])
    assert full["chat_id"] == "555123"

    public = store.public_watch(full)
    assert "chat_id" not in public
    # `linked` still conveys whether a chat is attached.
    assert public["linked"] is True


def test_public_watch_handles_missing_record():
    assert store.public_watch(None) is None

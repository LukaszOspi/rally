"""Bot command handling and alert matching, with Telegram's HTTP layer stubbed."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RALLY_DB"] = os.path.join(tempfile.mkdtemp(), "bot.db")

import pytest  # noqa: E402

from app import db, store, telegram  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    path = tmp_path / "bot.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init_db()

    with db.tx() as conn:
        for sid, name, country, cname, lat, lon, one_way in (
            (1, "Munich", "DE", "Germany", 48.14, 11.58, 0),
            (32, "Amsterdam", "NL", "Netherlands", 52.3, 4.85, 1),
            (20, "Madrid", "ES", "Spain", 40.4, -3.7, 1),
        ):
            conn.execute(
                "INSERT INTO stations(id,name,city,country,country_name,latitude,"
                "longitude,one_way,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, name, name, country, cname, lat, lon, one_way, db.utcnow()),
            )
    yield
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound messages instead of calling Telegram."""
    out = []
    monkeypatch.setattr(telegram, "TOKEN", "test-token")
    monkeypatch.setattr(telegram, "API", "https://example.invalid")
    monkeypatch.setattr(
        telegram, "_call",
        lambda method, **kw: (out.append((method, kw)) or {"ok": True}),
    )
    return out


def add_offer(key, start, end, date_from, date_to, bookable=1, price=129.01):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO offers(key,start_station_id,end_station_id,start_date,"
            "end_date,window_days,first_seen,last_seen,active,bookable,price,"
            "currency,detail_json) VALUES(?,?,?,?,?,7,?,?,1,?,?,'EUR',?)",
            (key, start, end, date_from, date_to, db.utcnow(), db.utcnow(),
             bookable, price,
             '{"vehicles":[{"name":"Surfer Suite","seats":4,"available":true}],'
             '"pickup":{"address":"A st","from":"09:00:00","to":"18:00:00"},'
             '"dropoff":{"address":"B st","from":"09:00:00","to":"18:00:00"}}'),
        )


def message(text, chat_id=555):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


# ── linking ──────────────────────────────────────────────────────────────────

def test_deep_link_start_binds_watch_to_chat(sent):
    watch = store.create_watch("Spain trips", {"start_countries": ["ES"]})
    assert watch["chat_id"] is None

    telegram.handle_update(message(f"/start {watch['id']}"))

    assert store.get_watch(watch["id"])["chat_id"] == "555"
    assert "Linked" in sent[-1][1]["text"]


def test_unknown_token_is_rejected(sent):
    telegram.handle_update(message("/start deadbeef"))
    assert "not valid" in sent[-1][1]["text"]


def test_watch_cannot_be_hijacked_by_another_chat(sent):
    watch = store.create_watch("Mine", {})
    telegram.handle_update(message(f"/start {watch['id']}", chat_id=111))
    telegram.handle_update(message(f"/start {watch['id']}", chat_id=222))

    assert store.get_watch(watch["id"])["chat_id"] == "111"
    assert "already linked" in sent[-1][1]["text"]


# ── commands ─────────────────────────────────────────────────────────────────

def test_list_pause_resume_delete(sent):
    watch = store.create_watch("Spain", {"start_countries": ["ES"]})
    telegram.handle_update(message(f"/start {watch['id']}"))

    telegram.handle_update(message("/list"))
    assert "Spain" in sent[-1][1]["text"]

    telegram.handle_update(message("/pause 1"))
    assert store.get_watch(watch["id"])["active"] is False

    telegram.handle_update(message("/resume 1"))
    assert store.get_watch(watch["id"])["active"] is True

    telegram.handle_update(message("/delete 1"))
    assert store.get_watch(watch["id"]) is None


def test_bad_index_gives_usage_not_a_crash(sent):
    telegram.handle_update(message("/pause 9"))
    assert "Usage" in sent[-1][1]["text"]


# ── alerting ─────────────────────────────────────────────────────────────────

def test_only_matching_offers_are_pushed(sent):
    watch = store.create_watch("Spain only", {"start_countries": ["ES"]})
    telegram.handle_update(message(f"/start {watch['id']}"))
    sent.clear()

    add_offer("20-1-a", 20, 1, "2026-09-01", "2026-09-08")   # Madrid, matches
    add_offer("32-1-a", 32, 1, "2026-09-01", "2026-09-08")   # Amsterdam, does not

    assert telegram.notify_matches(["20-1-a", "32-1-a"]) == 1
    text = sent[-1][1]["text"]
    assert "Madrid" in text and "Amsterdam" not in text


def test_the_same_offer_is_never_pushed_twice(sent):
    watch = store.create_watch("Everything", {})
    telegram.handle_update(message(f"/start {watch['id']}"))
    sent.clear()

    add_offer("20-1-b", 20, 1, "2026-09-01", "2026-09-08")
    assert telegram.notify_matches(["20-1-b"]) == 1
    assert telegram.notify_matches(["20-1-b"]) == 0


def test_paused_watch_receives_nothing(sent):
    watch = store.create_watch("Paused", {})
    telegram.handle_update(message(f"/start {watch['id']}"))
    store.update_watch(watch["id"], active=False)
    sent.clear()

    add_offer("20-1-c", 20, 1, "2026-09-01", "2026-09-08")
    assert telegram.notify_matches(["20-1-c"]) == 0


def test_unlinked_watch_receives_nothing(sent):
    store.create_watch("Never linked", {})
    add_offer("20-1-d", 20, 1, "2026-09-01", "2026-09-08")
    assert telegram.notify_matches(["20-1-d"]) == 0


def test_message_body_carries_the_practical_details(sent):
    watch = store.create_watch("All", {})
    telegram.handle_update(message(f"/start {watch['id']}"))
    sent.clear()

    add_offer("20-1-e", 20, 1, "2026-09-01", "2026-09-08")
    telegram.notify_matches(["20-1-e"])

    text = sent[-1][1]["text"]
    for expected in ("Madrid", "Munich", "2026-09-01", "A st", "B st",
                     "Surfer Suite", "129"):
        assert expected in text, f"missing {expected!r}"

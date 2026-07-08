"""Route-level tests for server.py, driven through Flask's test client
(headless: server.py sets the SDL dummy video/audio drivers before
importing pygame, so this needs no real display)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server


def make_client():
    server.app.testing = True
    return server.app.test_client()


def create_room(client, ip, **form):
    """POST /new with a dedicated fake client IP so tests never contend for
    the shared /new rate-limit quota, and return the new room_id."""
    form.setdefault("time_limit", "0")
    resp = client.post("/new", data=form, headers={"X-Forwarded-For": ip})
    assert resp.status_code == 302
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_new_game_rate_limit_blocks_after_threshold():
    server._new_room_hits.clear()
    client = make_client()

    statuses = [client.post("/new", data={"time_limit": "0"}).status_code
                for _ in range(server.NEW_ROOM_RATE_LIMIT)]
    assert all(status == 302 for status in statuses)

    blocked = client.post("/new", data={"time_limit": "0"})
    assert blocked.status_code == 429


def test_new_game_rate_limit_is_per_client_ip():
    server._new_room_hits.clear()
    client = make_client()

    for _ in range(server.NEW_ROOM_RATE_LIMIT):
        client.post("/new", data={"time_limit": "0"})
    assert client.post("/new", data={"time_limit": "0"}).status_code == 429

    other_ip = client.post(
        "/new", data={"time_limit": "0"},
        headers={"X-Forwarded-For": "203.0.113.42"},
    )
    assert other_ip.status_code == 302


def test_resign_sets_winner_to_opponent():
    client = make_client()
    room_id = create_room(client, "198.51.100.1")

    resp = client.post(f"/resign/{room_id}", json={"color": "white"})
    assert resp.status_code == 200

    room = server.rooms[room_id]
    assert room["winner"] == "black"
    assert room["win_reason"] == "resignation"
    assert room["clock_since"] is None


def test_resign_rejected_once_game_over():
    client = make_client()
    room_id = create_room(client, "198.51.100.2")

    client.post(f"/resign/{room_id}", json={"color": "white"})
    second = client.post(f"/resign/{room_id}", json={"color": "black"})

    assert second.status_code == 400
    room = server.rooms[room_id]
    assert room["winner"] == "black"          # unchanged by the rejected 2nd call
    assert room["win_reason"] == "resignation"


def test_draw_offer_then_accept_ends_in_draw():
    client = make_client()
    room_id = create_room(client, "198.51.100.3")

    offer = client.post(f"/draw_offer/{room_id}", json={"color": "white"})
    assert offer.status_code == 200
    assert server.rooms[room_id]["draw_offered_by"] == "white"

    respond = client.post(f"/draw_respond/{room_id}", json={"accept": True})
    assert respond.status_code == 200

    room = server.rooms[room_id]
    assert room["winner"] == "draw"
    assert room["win_reason"] == "agreement"
    assert room["draw_offered_by"] is None


def test_draw_offer_then_decline_clears_offer():
    client = make_client()
    room_id = create_room(client, "198.51.100.4")

    client.post(f"/draw_offer/{room_id}", json={"color": "black"})
    respond = client.post(f"/draw_respond/{room_id}", json={"accept": False})
    assert respond.status_code == 200

    room = server.rooms[room_id]
    assert room["draw_offered_by"] is None
    assert room["winner"] is None


def test_draw_offer_rejected_in_ai_room():
    client = make_client()
    room_id = create_room(client, "198.51.100.5", ai="1", difficulty="easy")

    resp = client.post(f"/draw_offer/{room_id}", json={"color": "white"})
    assert resp.status_code == 400
    assert server.rooms[room_id]["draw_offered_by"] is None


def test_deduct_clock_adds_increment_after_time_spent():
    room = server.make_room(time_limit=100, increment=5)
    room["clock_since"] = time.time() - 10   # pretend 10s have elapsed
    server.deduct_clock(room)

    # 100 - 10 (elapsed) + 5 (increment) = 95, with slack for real test runtime.
    assert 94.5 <= room["white_time"] <= 95.5

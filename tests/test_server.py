"""Route-level tests for server.py, driven through Flask's test client
(headless: server.py sets the SDL dummy video/audio drivers before
importing pygame, so this needs no real display)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server


def make_client():
    server.app.testing = True
    return server.app.test_client()


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

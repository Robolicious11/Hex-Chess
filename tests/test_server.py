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


def pixel_for(label, flip=False):
    """Pixel coordinates for a board label, matching exactly how the real
    client computes them (and how render_room places a flipped piece)."""
    g = server.Game(size=server.BOARD_SIZE)
    q, r = g.from_label(label)
    if flip:
        q, r = -q, -r
    x, y, _ = g.to_pixel(q, r, server.WIDTH, server.HEIGHT, zoom=server.ZOOM)
    return x, y


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


def test_resign_rejected_on_behalf_of_ai():
    client = make_client()
    room_id = create_room(client, "198.51.100.6", ai="1", difficulty="easy")

    resp = client.post(f"/resign/{room_id}", json={"color": "black"})  # black == AI here
    assert resp.status_code == 400

    room = server.rooms[room_id]
    assert room["winner"] is None             # AI cannot be made to "resign"

    # The human (white) can still resign normally.
    resp2 = client.post(f"/resign/{room_id}", json={"color": "white"})
    assert resp2.status_code == 200
    assert server.rooms[room_id]["winner"] == "black"


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


def test_click_with_flip_maps_back_to_true_board_square():
    client = make_client()
    room_id = create_room(client, "198.51.100.7")

    x, y = pixel_for("C2", flip=True)
    resp = client.post(f"/click/{room_id}", json={
        "x": x, "y": y, "imgW": server.WIDTH, "imgH": server.HEIGHT, "flip": True,
    })
    assert resp.status_code == 200
    assert resp.get_json()["event"] == "select"

    room = server.rooms[room_id]
    assert room["selected"] == room["game"].from_label("C2")


def test_frame_renders_with_flip_enabled():
    client = make_client()
    room_id = create_room(client, "198.51.100.8")

    resp = client.get(f"/frame/{room_id}?flip=1")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_undo_reverts_last_move_in_2p_room():
    client = make_client()
    room_id = create_room(client, "198.51.100.9")

    x1, y1 = pixel_for("C2")
    x2, y2 = pixel_for("C4")
    client.post(f"/click/{room_id}", json={"x": x1, "y": y1, "imgW": server.WIDTH, "imgH": server.HEIGHT})
    move_resp = client.post(f"/click/{room_id}", json={"x": x2, "y": y2, "imgW": server.WIDTH, "imgH": server.HEIGHT})
    assert move_resp.get_json()["event"] == "move"

    room = server.rooms[room_id]
    assert len(room["history"]) == 1
    assert room["game"].turn == "black"

    undo_resp = client.post(f"/undo/{room_id}")
    assert undo_resp.status_code == 200

    room = server.rooms[room_id]
    assert len(room["history"]) == 0
    assert room["game"].turn == "white"
    c2, c4 = room["game"].from_label("C2"), room["game"].from_label("C4")
    assert room["game"].board[c2] is not None and room["game"].board[c2].name == "pawn"
    assert room["game"].board[c4] is None


def test_undo_rejected_when_nothing_to_undo():
    client = make_client()
    room_id = create_room(client, "198.51.100.10")

    resp = client.post(f"/undo/{room_id}")
    assert resp.status_code == 400


def test_undo_rejected_while_ai_thinking():
    client = make_client()
    room_id = create_room(client, "198.51.100.11", ai="1", difficulty="easy")

    room = server.rooms[room_id]
    room["undo_stack"].append(server.snapshot_room_state(room))  # pretend a move happened
    room["ai_thinking"] = True

    resp = client.post(f"/undo/{room_id}")
    assert resp.status_code == 400
    assert len(room["undo_stack"]) == 1   # untouched


def test_undo_pops_two_plies_in_ai_room():
    client = make_client()
    room_id = create_room(client, "198.51.100.12", ai="1", difficulty="easy")

    room = server.rooms[room_id]
    game = room["game"]

    room["undo_stack"].append(server.snapshot_room_state(room))
    w_src = game.from_label("C2")
    w_dst = game.legal_moves(w_src)[0]
    game.move(w_src, w_dst)
    server.record_move(room, "white", "pawn", w_src, w_dst)

    room["undo_stack"].append(server.snapshot_room_state(room))
    b_src = next(pos for pos, p in game.board.items() if p and p.owner == "black" and p.name == "pawn")
    b_dst = game.legal_moves(b_src)[0]
    game.move(b_src, b_dst)
    server.record_move(room, "black", "pawn", b_src, b_dst)

    assert len(room["history"]) == 2
    assert game.turn == "white"

    resp = client.post(f"/undo/{room_id}")
    assert resp.status_code == 200

    room = server.rooms[room_id]
    assert len(room["history"]) == 0
    assert room["game"].turn == "white"
    assert room["game"].board[w_src] is not None and room["game"].board[w_src].name == "pawn"
    assert room["game"].board[w_dst] is None


def test_preview_image_renders_a_valid_png():
    client = make_client()
    resp = client.get("/preview.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_new_game_stores_valid_theme():
    client = make_client()
    room_id = create_room(client, "198.51.100.13", theme="ocean")
    assert server.rooms[room_id]["theme"] == "ocean"


def test_new_game_falls_back_to_default_theme_for_invalid_value():
    client = make_client()
    room_id = create_room(client, "198.51.100.14", theme="not-a-real-theme")
    assert server.rooms[room_id]["theme"] == server.DEFAULT_BOARD_THEME


def test_frame_renders_with_each_board_theme():
    client = make_client()
    for theme in server.BOARD_THEMES:
        room_id = create_room(client, f"198.51.100.{15 + list(server.BOARD_THEMES).index(theme)}", theme=theme)
        resp = client.get(f"/frame/{room_id}")
        assert resp.status_code == 200
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

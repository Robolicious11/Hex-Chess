import os
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

import pygame
import math
import io
import time
import threading
import uuid
import base64
from flask import Flask, Response, request, jsonify, render_template_string, redirect, url_for
from my_hexchess import Game

app = Flask(__name__)

WIDTH, HEIGHT = 700, 580
ZOOM = 0.6
DRAW_SCALE = 0.95

FONT_PATH = "DejaVuSans.ttf"

pygame.init()
surface = pygame.Surface((WIDTH, HEIGHT))

label_font = pygame.font.Font(FONT_PATH, 14)
_piece_font_cache = {}

render_lock = threading.Lock()

PIECE_SYMBOLS = {
    "white": {"king": "♔", "queen": "♕", "bishop": "♗", "knight": "♘", "pawn": "♙"},
    "black": {"king": "♚", "queen": "♛", "bishop": "♝", "knight": "♞", "pawn": "♟"}
}

# ---------------- ROOMS ----------------

rooms = {}

def make_room():
    return {
        "game": Game(size=4),
        "selected": None,
        "legal_moves": [],
        "last_move": None,
        "animation": None,
        "players": {"white": None, "black": None}
    }


# ---------------- FONT ----------------

def get_piece_font(size):
    if size not in _piece_font_cache:
        _piece_font_cache[size] = pygame.font.Font(FONT_PATH, size)
    return _piece_font_cache[size]


# ---------------- DRAW ----------------

def draw_hex(x, y, size, color):
    points = [
        (x + size * math.cos(math.radians(60 * i)),
         y + size * math.sin(math.radians(60 * i)))
        for i in range(6)
    ]
    pygame.draw.polygon(surface, color, points)
    pygame.draw.polygon(surface, (0, 0, 0), points, 1)


def render_room(room):
    game = room["game"]
    selected = room["selected"]
    legal_moves = room["legal_moves"]
    last_move = room.get("last_move")

    surface.fill((255, 255, 255))

    for (q, r), piece in game.board.items():
        x, y, tile_size = game.to_pixel(q, r, WIDTH, HEIGHT, zoom=ZOOM)
        hex_radius = int(tile_size * DRAW_SCALE)

        base = (200, 200, 200) if (q + r) % 2 == 0 else (160, 160, 160)

        color = base
        if last_move and (q, r) in last_move:
            color = (180, 180, 255)
        if selected == (q, r):
            color = (255, 230, 80)
        elif (q, r) in legal_moves:
            color = (140, 240, 140)

        draw_hex(x, y, hex_radius, color)

        # ✅ Draw piece normally
        if piece:
            size = round(hex_radius * 1.3)
            pf = get_piece_font(size)
            sym = PIECE_SYMBOLS[piece.owner][piece.name]

            txt = pf.render(sym, True, (0, 0, 0))
            rect = txt.get_rect(center=(round(x), round(y)))
            surface.blit(txt, rect)

        # ✅ Labels (sharp)
        label = game.to_label(q, r)
        lt = label_font.render(label, False, (80, 80, 80))
        rect = lt.get_rect(center=(round(x), round(y + hex_radius * 0.65)))
        surface.blit(lt, rect)

    # ✅ Animation (draw on top)
    anim = room.get("animation")
    if anim:
        t = (time.time() - anim["start"]) / 0.2

        if t >= 1:
            room["game"].board[anim["to"]] = anim["piece"]
            room["animation"] = None
        else:
            fx, fy, _ = game.to_pixel(*anim["from"], WIDTH, HEIGHT, zoom=ZOOM)
            tx, ty, _ = game.to_pixel(*anim["to"], WIDTH, HEIGHT, zoom=ZOOM)

            nx = fx + (tx - fx) * t
            ny = fy + (ty - fy) * t

            size = 50
            pf = get_piece_font(size)
            sym = PIECE_SYMBOLS[anim["piece"].owner][anim["piece"].name]

            txt = pf.render(sym, True, (0, 0, 0))
            surface.blit(txt, txt.get_rect(center=(nx, ny)))


def get_frame(room):
    with render_lock:
        render_room(room)
        buf = io.BytesIO()
        pygame.image.save(surface, buf, "frame.png")
        return buf.getvalue()


# ---------------- ROUTES ----------------

@app.route('/')
def index():
    return render_template_string("""
    <h1>Hex Chess</h1>
    /new
        <button>Create Game</button>
    </form>
    """)


@app.route('/new', methods=['POST'])
def new():
    room_id = uuid.uuid4().hex[:8]
    rooms[room_id] = make_room()
    return redirect(f"/game/{room_id}")


@app.route('/game/<room_id>')
def game(room_id):
    return render_template_string("""
    <h2>Hex Chess</h2>
    <img id="board">
    <p id="role"></p>

    <script>
    const ROOM = "{{room}}";
    const img = document.getElementById("board");

    function refresh() {
        img.src = "/frame/" + ROOM + "?t=" + Date.now();
        setTimeout(refresh, 100);
    }
    refresh();

    img.onclick = e => {
        const r = img.getBoundingClientRect();
        fetch("/click/" + ROOM, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                x: e.clientX - r.left,
                y: e.clientY - r.top,
                imgW: r.width,
                imgH: r.height
            })
        });
    };
    </script>
    """, room=room_id)


@app.route('/frame/<room_id>')
def frame(room_id):
    return Response(get_frame(rooms[room_id]), mimetype='image/png')


@app.route('/click/<room_id>', methods=['POST'])
def click(room_id):
    room = rooms[room_id]
    game = room["game"]

    user = request.remote_addr

    # ✅ assign roles
    if room["players"]["white"] is None:
        room["players"]["white"] = user
    elif room["players"]["black"] is None and user != room["players"]["white"]:
        room["players"]["black"] = user

    # ✅ restrict movement
    if room["players"][game.turn] != user:
        return jsonify({"ok": False})

    data = request.json
    mx = int(data["x"])
    my = int(data["y"])

    q, r = game.from_pixel(mx, my, WIDTH, HEIGHT, zoom=ZOOM)

    if (q, r) not in game.board:
        room["selected"] = None
        room["legal_moves"] = []
        return jsonify({"ok": True})

    piece = game.board.get((q, r))

    if room["selected"] is None:
        if piece and piece.owner == game.turn:
            room["selected"] = (q, r)
            room["legal_moves"] = game.legal_moves((q, r))
    else:
        if (q, r) in room["legal_moves"]:
            src = room["selected"]

            room["animation"] = {
                "from": src,
                "to": (q, r),
                "piece": game.board[src],
                "start": time.time()
            }

            game.board[src] = None
            game.turn = "black" if game.turn == "white" else "white"
            room["last_move"] = (src, (q, r))

        room["selected"] = None
        room["legal_moves"] = []

    return jsonify({"ok": True})


# ---------------- RUN ----------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
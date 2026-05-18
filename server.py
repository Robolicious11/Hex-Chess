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

ZOOM = 0.6
DRAW_SCALE = 0.95
WIDTH, HEIGHT = 700, 580

FONT_PATH = "DejaVuSans.ttf"

PIECE_SYMBOLS = {
    "white": {"king": "♔", "queen": "♕", "bishop": "♗", "knight": "♘", "pawn": "♙"},
    "black": {"king": "♚", "queen": "♛", "bishop": "♝", "knight": "♞", "pawn": "♟"}
}

pygame.init()
surface = pygame.Surface((WIDTH, HEIGHT))

label_font = pygame.font.Font(FONT_PATH, 12)
_piece_font_cache = {}

render_lock = threading.Lock()

# --- Rooms ---
rooms = {}
rooms_lock = threading.Lock()


def make_room():
    return {
        "game": Game(size=4),
        "selected": None,
        "legal_moves": []
    }


def get_room(room_id):
    return rooms.get(room_id)


# --- DRAWING ---

def get_piece_font(size):
    if size not in _piece_font_cache:
        _piece_font_cache[size] = pygame.font.Font(FONT_PATH, size)
    return _piece_font_cache[size]


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

    surface.fill((255, 255, 255))

    for (q, r), piece in game.board.items():
        x, y, tile_size = game.to_pixel(q, r, WIDTH, HEIGHT, zoom=ZOOM)
        hex_radius = int(tile_size * DRAW_SCALE)

        if selected == (q, r):
            color = (255, 220, 50)
        elif (q, r) in legal_moves:
            color = (130, 220, 130)
        else:
            color = (180, 180, 180)

        draw_hex(x, y, hex_radius, color)

        # ✅ PIECES (sharp)
        if piece:
            size = round(hex_radius * 1.3)
            pf = get_piece_font(size)
            sym = PIECE_SYMBOLS[piece.owner][piece.name]

            txt = pf.render(sym, True, (0, 0, 0))
            rect = txt.get_rect(center=(round(x), round(y)))
            surface.blit(txt, rect)

        # ✅ LABELS (NO BLUR)
        label = game.to_label(q, r)
        lt = label_font.render(label, False, (80, 80, 80))
        rect = lt.get_rect(center=(round(x), round(y + hex_radius * 0.65)))
        surface.blit(lt, rect)


def get_frame_bytes(room):
    with render_lock:
        render_room(room)
        buf = io.BytesIO()
        pygame.image.save(surface, buf, "frame.png")
        buf.seek(0)
        return buf.read()


# --- ROUTES ---

@app.route('/')
def index():
    return render_template_string("""
    <h1>HEX CHESS</h1>
    /new
        <button type="submit">Create Game</button>
    </form>
    """)


@app.route('/new', methods=['POST'])
def new_game():
    room_id = uuid.uuid4().hex[:10]
    rooms[room_id] = make_room()
    return redirect(url_for('game_page', room_id=room_id))


@app.route('/game/<room_id>')
def game_page(room_id):
    first_frame = base64.b64encode(get_frame_bytes(rooms[room_id])).decode('utf-8')

    return render_template_string("""
    <h2>HEX CHESS</h2>
    data:image/png;base64,{{ frame }}

    <script>
    const ROOM = "{{ room_id }}";
    const img = document.getElementById("game");

    function refresh() {
        img.src = "/frame/" + ROOM + "?t=" + Date.now();
        setTimeout(refresh, 100);
    }
    refresh();

    img.onclick = function(e) {
        const rect = img.getBoundingClientRect();
        fetch("/click/" + ROOM, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                x: e.clientX - rect.left,
                y: e.clientY - rect.top,
                imgW: rect.width,
                imgH: rect.height
            })
        });
    };
    </script>
    """, room_id=room_id, frame=first_frame)


@app.route('/frame/<room_id>')
def frame(room_id):
    return Response(get_frame_bytes(rooms[room_id]), mimetype='image/png')


@app.route('/click/<room_id>', methods=['POST'])
def click(room_id):
    room = rooms[room_id]

    data = request.json
    mx = int(data["x"])
    my = int(data["y"])

    q, r = room["game"].from_pixel(mx, my, WIDTH, HEIGHT, zoom=ZOOM)

    if (q, r) not in room["game"].board:
        room["selected"] = None
        room["legal_moves"] = []
        return jsonify({"ok": True})

    piece = room["game"].board.get((q, r))

    if room["selected"] is None:
        if piece and piece.owner == room["game"].turn:
            room["selected"] = (q, r)
            room["legal_moves"] = room["game"].legal_moves((q, r))
    else:
        if (q, r) in room["legal_moves"]:
            room["game"].move(room["selected"], (q, r))

        room["selected"] = None
        room["legal_moves"] = []

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
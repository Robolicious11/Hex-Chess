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
#surface = pygame.display.set_mode((WIDTH, HEIGHT))
surface = pygame.Surface((WIDTH, HEIGHT))
label_font = pygame.font.Font(FONT_PATH, 10)
_piece_font_cache = {}
render_lock = threading.Lock()

# --- Room storage ---
rooms = {}
rooms_lock = threading.Lock()


def make_room(time_limit=180):
    deadline = time.time() + time_limit if time_limit > 0 else None
    return {
        "game": Game(size=4),
        "selected": None,
        "legal_moves": [],
        "winner": None,
        "win_reason": None,
        "pending_promotion": None,
        "time_limit": time_limit,
        "move_deadline": deadline,
        "lock": threading.Lock(),
        "created": time.time(),
    }


def reset_timer(room):
    """Reset the move clock for the next player. Call under room lock."""
    if room["time_limit"] > 0:
        room["move_deadline"] = time.time() + room["time_limit"]


def check_timer_expiry(room):
    """Forfeit the current player if their time is up. Call under room lock."""
    if room["winner"] or room["pending_promotion"]:
        return
    deadline = room["move_deadline"]
    if deadline is None:
        return
    if time.time() > deadline:
        current = room["game"].turn
        room["winner"] = "black" if current == "white" else "white"
        room["win_reason"] = "timeout"
        room["move_deadline"] = None


def check_game_over(game):
    """
    Called after a move has been made (turn already flipped to next player).
    Returns ("checkmate", winner_color), ("stalemate", None), or None.
    """
    current = game.turn
    if game.is_checkmate(current):
        opponent = "black" if current == "white" else "white"
        return ("checkmate", opponent)
    if game.is_stalemate(current):
        return ("stalemate", None)
    return None


def get_room(room_id):
    with rooms_lock:
        return rooms.get(room_id)


# --- Drawing helpers ---

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

        if piece:
            pf = get_piece_font(int(hex_radius * 1.5))
            sym = PIECE_SYMBOLS[piece.owner][piece.name]
            t = pf.render(sym, True, (0, 0, 0))
            surface.blit(t, t.get_rect(center=(x, y)))

        lt = label_font.render(game.to_label(q, r), False, (80, 80, 80))
        surface.blit(lt, lt.get_rect(center=(x, y + hex_radius * 0.65)))

    winner = room.get("winner")
    if winner:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 160))
        surface.blit(overlay, (0, 0))
        big_font = pygame.font.SysFont("dejavusans", 40, bold=True)
        sub_font = pygame.font.SysFont("dejavusans", 20)
        if winner == "draw":
            title = big_font.render("Stalemate — Draw!", True, (200, 200, 255))
        elif room.get("win_reason") == "timeout":
            loser = "black" if winner == "white" else "white"
            sym = "♔" if winner == "white" else "♚"
            title = big_font.render(f"{sym}  {loser.capitalize()} ran out of time!  {sym}", True, (255, 220, 50))
        else:
            sym = "♔" if winner == "white" else "♚"
            title = big_font.render(f"{sym}  {winner.capitalize()} wins by checkmate!  {sym}", True, (255, 220, 50))
        hint = sub_font.render("Press Reset Game to play again", True, (200, 200, 200))
        surface.blit(title, title.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 28)))
        surface.blit(hint,  hint.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 30)))
    else:
        in_check = game.is_in_check(game.turn)
        bg = (180, 30, 30) if in_check else ((255, 255, 255) if game.turn == "white" else (30, 30, 30))
        fg = (255, 255, 255) if (in_check or game.turn == "black") else (0, 0, 0)
        turn_word = "White" if game.turn == "white" else "Black"
        label = f"{turn_word} is in CHECK!" if in_check else f"{turn_word}'s turn"
        font = pygame.font.SysFont("dejavusans", 18, bold=True)
        box_w = 210 if in_check else 160
        pygame.draw.rect(surface, bg, (10, 10, box_w, 36), border_radius=8)
        pygame.draw.rect(surface, (0, 0, 0), (10, 10, box_w, 36), 2, border_radius=8)
        t = font.render(label, True, fg)
        surface.blit(t, t.get_rect(center=(10 + box_w // 2, 28)))

        # --- Timer display (top-right) ---
        deadline = room.get("move_deadline")
        time_limit = room.get("time_limit", 0)
        if deadline is not None and time_limit > 0:
            remaining = max(0.0, deadline - time.time())
            fraction = remaining / time_limit
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            timer_str = f"{mins}:{secs:02d}"
            if fraction > 0.5:
                t_color = (40, 180, 40)
            elif fraction > 0.25:
                t_color = (220, 170, 20)
            else:
                t_color = (210, 50, 50)
            bar_w = 90
            bar_x = WIDTH - bar_w - 10
            pygame.draw.rect(surface, t_color, (bar_x, 10, bar_w, 36), border_radius=8)
            pygame.draw.rect(surface, (0, 0, 0), (bar_x, 10, bar_w, 36), 2, border_radius=8)
            t_font = pygame.font.SysFont("dejavusans", 20, bold=True)
            t_surf = t_font.render(timer_str, True, (255, 255, 255))
            surface.blit(t_surf, t_surf.get_rect(center=(bar_x + bar_w // 2, 28)))


def get_frame_bytes(room):
    with render_lock:
        with room["lock"]:
            check_timer_expiry(room)
            render_room(room)
        buf = io.BytesIO()
        pygame.image.save(surface, buf, "frame.png")
        buf.seek(0)
        return buf.read()


# --- Routes ---

@app.route('/')
def index():
    return render_template_string(LANDING_HTML)


@app.route('/new', methods=['POST'])
def new_game():
    room_id = uuid.uuid4().hex[:10]
    try:
        time_limit = int(request.form.get('time_limit', 180))
    except (ValueError, TypeError):
        time_limit = 180
    with rooms_lock:
        rooms[room_id] = make_room(time_limit=time_limit)
    return redirect(url_for('game_page', room_id=room_id))


@app.route('/game/<room_id>')
def game_page(room_id):
    room = get_room(room_id)
    if room is None:
        return "Game not found. <a href='/'>Create a new game</a>", 404
    first_frame = base64.b64encode(get_frame_bytes(room)).decode('utf-8')
    return render_template_string(GAME_HTML, room_id=room_id, first_frame=first_frame)


@app.route('/frame/<room_id>')
def frame(room_id):
    room = get_room(room_id)
    if room is None:
        return '', 404
    data = get_frame_bytes(room)
    return Response(data, mimetype='image/png', headers={'Cache-Control': 'no-store'})


@app.route('/click/<room_id>', methods=['POST'])
def click(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404

    data = request.json
    img_w = data.get('imgW', WIDTH)
    img_h = data.get('imgH', HEIGHT)
    mx = int(data['x'] * WIDTH / img_w)
    my = int(data['y'] * HEIGHT / img_h)

    with room["lock"]:
        check_timer_expiry(room)
        if room["winner"] or room["pending_promotion"]:
            return jsonify({'ok': True})

        game = room["game"]
        q, r = game.from_pixel(mx, my, WIDTH, HEIGHT, zoom=ZOOM)

        if (q, r) not in game.board:
            room["selected"] = None
            room["legal_moves"] = []
            return jsonify({'ok': True})

        piece = game.board.get((q, r))

        if room["selected"] is None:
            if piece and piece.owner == game.turn:
                room["selected"] = (q, r)
                room["legal_moves"] = game.legal_moves(room["selected"])
        else:
            if (q, r) in room["legal_moves"]:
                src = room["selected"]
                moving_piece = game.board[src]
                is_promo = (
                    moving_piece.name == "pawn"
                    and game.is_promotion_square((q, r), moving_piece.owner)
                )
                if is_promo:
                    game.board[(q, r)] = moving_piece
                    game.board[src] = None
                    moving_piece.has_moved = True
                    room["pending_promotion"] = (q, r)
                else:
                    game.move(src, (q, r))
                    reset_timer(room)
                    result = check_game_over(game)
                    if result:
                        kind, w = result
                        room["winner"] = w if kind == "checkmate" else "draw"
                        room["win_reason"] = kind
            room["selected"] = None
            room["legal_moves"] = []

    return jsonify({'ok': True})


@app.route('/reset/<room_id>', methods=['POST'])
def reset(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    with room["lock"]:
        room["game"] = Game(size=4)
        room["selected"] = None
        room["legal_moves"] = []
        room["winner"] = None
        room["win_reason"] = None
        room["pending_promotion"] = None
        reset_timer(room)
    return jsonify({'ok': True})


@app.route('/state/<room_id>')
def state(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    with room["lock"]:
        return jsonify({
            'pending_promotion': room["pending_promotion"] is not None,
            'turn': room["game"].turn,
        })


@app.route('/promote/<room_id>', methods=['POST'])
def promote(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    data = request.json
    piece_name = data.get('piece', 'queen')
    if piece_name not in ('queen', 'bishop', 'knight'):
        return jsonify({'ok': False, 'error': 'invalid piece'}), 400
    with room["lock"]:
        pos = room["pending_promotion"]
        if pos is None:
            return jsonify({'ok': False, 'error': 'no promotion pending'}), 400
        game = room["game"]
        game.board[pos].name = piece_name
        room["pending_promotion"] = None
        game.turn = "black" if game.turn == "white" else "white"
        reset_timer(room)
        result = check_game_over(game)
        if result:
            kind, w = result
            room["winner"] = w if kind == "checkmate" else "draw"
            room["win_reason"] = kind
    return jsonify({'ok': True})


# --- HTML Templates ---

LANDING_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hex Chess</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #1a1a2e; display: flex; flex-direction: column;
           align-items: center; justify-content: center; min-height: 100vh;
           font-family: sans-serif; color: #eee; }
    h1 { font-size: 2.5rem; letter-spacing: 4px; margin-bottom: 12px; }
    p  { color: #aaa; margin-bottom: 36px; font-size: 1rem; }
    form button {
      padding: 14px 40px; font-size: 1.1rem; letter-spacing: 2px;
      background: #2980b9; color: #fff; border: none; border-radius: 8px;
      cursor: pointer; transition: background 0.2s;
    }
    form button:hover { background: #3498db; }
  </style>
</head>
<body>
  <h1>HEX CHESS</h1>
  <p>Create a game and share the link with a friend to play.</p>
  <form method="POST" action="/new">
    <div style="margin-bottom:20px; display:flex; flex-direction:column; align-items:center; gap:8px;">
      <label for="time_limit" style="color:#aaa; font-size:0.9rem; letter-spacing:1px;">TIME PER MOVE</label>
      <select name="time_limit" id="time_limit" style="
        padding:10px 20px; font-size:1rem; background:#16213e; color:#eee;
        border:1px solid #2980b9; border-radius:8px; cursor:pointer; outline:none;
      ">
        <option value="60">1 minute</option>
        <option value="180" selected>3 minutes</option>
        <option value="300">5 minutes</option>
        <option value="600">10 minutes</option>
        <option value="0">No limit</option>
      </select>
    </div>
    <button type="submit">Create New Game</button>
  </form>
</body>
</html>'''


GAME_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hex Chess</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #1a1a2e; display: flex; flex-direction: column;
           align-items: center; justify-content: center; min-height: 100vh; }
    h1 { color: #eee; font-family: sans-serif; margin-bottom: 8px;
         font-size: 1.3rem; letter-spacing: 2px; }
    #share-box {
      display: flex; align-items: center; gap: 8px;
      margin-bottom: 12px;
    }
    #share-url {
      background: #16213e; color: #aee; border: 1px solid #2980b9;
      border-radius: 6px; padding: 6px 12px; font-size: 0.8rem;
      font-family: monospace; width: 320px; overflow: hidden;
      white-space: nowrap; text-overflow: ellipsis;
    }
    #copy-btn {
      padding: 6px 14px; background: #2980b9; color: #fff; border: none;
      border-radius: 6px; cursor: pointer; font-size: 0.8rem;
      transition: background 0.2s;
    }
    #copy-btn:hover { background: #3498db; }
    #game { cursor: pointer; border-radius: 8px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5);
            max-width: 100%; max-height: 78vh; }
    #reset-btn {
      margin-top: 12px; padding: 8px 24px;
      background: #c0392b; color: #fff; border: none;
      border-radius: 6px; font-size: 0.9rem; font-family: sans-serif;
      cursor: pointer; letter-spacing: 1px; transition: background 0.2s;
    }
    #reset-btn:hover { background: #e74c3c; }
    #promo-overlay {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.75); z-index: 100;
      align-items: center; justify-content: center;
    }
    #promo-overlay.active { display: flex; }
    #promo-box {
      background: #1e2a3a; border: 2px solid #2980b9; border-radius: 14px;
      padding: 32px 40px; text-align: center; color: #eee; font-family: sans-serif;
    }
    #promo-box h2 { font-size: 1.4rem; letter-spacing: 2px; margin-bottom: 8px; }
    #promo-box p  { color: #aaa; margin-bottom: 24px; font-size: 0.95rem; }
    .promo-btn {
      padding: 14px 22px; margin: 0 8px; font-size: 1.8rem;
      background: #2980b9; color: #fff; border: none; border-radius: 10px;
      cursor: pointer; transition: background 0.2s, transform 0.1s;
      line-height: 1;
    }
    .promo-btn:hover { background: #3498db; transform: scale(1.08); }
    .promo-label { display: block; font-size: 0.7rem; margin-top: 4px;
                   letter-spacing: 1px; color: #cde; }
  </style>
</head>
<body>
  <h1>HEX CHESS</h1>
  <div id="share-box">
    <span id="share-url"></span>
    <button id="copy-btn">Copy Link</button>
  </div>
  <img id="game" src="data:image/png;base64,{{ first_frame }}" draggable="false">
  <button id="reset-btn">Reset Game</button>

  <!-- Promotion dialog -->
  <div id="promo-overlay">
    <div id="promo-box">
      <h2>PAWN PROMOTION</h2>
      <p>Choose a piece to promote to:</p>
      <div id="promo-buttons"></div>
    </div>
  </div>

  <script>
    const ROOM = "{{ room_id }}";
    const shareUrl = window.location.href;
    document.getElementById('share-url').textContent = shareUrl;

    document.getElementById('copy-btn').addEventListener('click', function() {
      navigator.clipboard.writeText(shareUrl).then(() => {
        this.textContent = 'Copied!';
        setTimeout(() => this.textContent = 'Copy Link', 1500);
      });
    });

    const SYMBOLS = {
      white: { queen: '♕', bishop: '♗', knight: '♘' },
      black: { queen: '♛', bishop: '♝', knight: '♞' }
    };

    let promoActive = false;

    async function choosePromo(piece) {
      promoActive = false;
      document.getElementById('promo-overlay').classList.remove('active');
      await fetch('/promote/' + ROOM, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ piece })
      });
    }

    async function checkState() {
      try {
        const resp = await fetch('/state/' + ROOM + '?t=' + Date.now());
        const data = await resp.json();
        if (data.pending_promotion && !promoActive) {
          promoActive = true;
          const syms = SYMBOLS[data.turn];
          const container = document.getElementById('promo-buttons');
          container.innerHTML = '';
          [['queen','Queen'], ['bishop','Bishop'], ['knight','Knight']].forEach(([p, label]) => {
            const btn = document.createElement('button');
            btn.className = 'promo-btn';
            btn.innerHTML = syms[p] + '<span class="promo-label">' + label + '</span>';
            btn.onclick = () => choosePromo(p);
            container.appendChild(btn);
          });
          document.getElementById('promo-overlay').classList.add('active');
        } else if (!data.pending_promotion && promoActive) {
          promoActive = false;
          document.getElementById('promo-overlay').classList.remove('active');
        }
      } catch(e) {}
    }

    const img = document.getElementById('game');
    let stateCounter = 0;
    function refresh() {
      const next = new Image();
      next.onload = function() {
        img.src = next.src;
        stateCounter++;
        if (stateCounter % 5 === 0) checkState();
        setTimeout(refresh, 80);
      };
      next.onerror = function() { setTimeout(refresh, 300); };
      next.src = '/frame/' + ROOM + '?t=' + Date.now();
    }
    setTimeout(refresh, 80);

    document.getElementById('reset-btn').addEventListener('click', function() {
      promoActive = false;
      document.getElementById('promo-overlay').classList.remove('active');
      fetch('/reset/' + ROOM, { method: 'POST' });
    });

    img.addEventListener('click', function(e) {
      if (promoActive) return;
      const rect = img.getBoundingClientRect();
      fetch('/click/' + ROOM, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          x: e.clientX - rect.left,
          y: e.clientY - rect.top,
          imgW: rect.width,
          imgH: rect.height
        })
      });
    });
  </script>
</body>
</html>'''


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)

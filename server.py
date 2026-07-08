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
import random
import copy
from flask import Flask, Response, request, jsonify, render_template_string, redirect, url_for
from my_hexchess import Game

app = Flask(__name__)

ZOOM        = 0.6
DRAW_SCALE  = 0.95
WIDTH       = 700
HEIGHT      = 580
FONT_PATH   = "DejaVuSans.ttf"
BOARD_SIZE  = 4

PIECE_VALUES = {"queen": 9, "bishop": 3, "knight": 3, "pawn": 1, "king": 0}
PIECE_SYMBOLS = {
    "white": {"king": "♔", "queen": "♕", "bishop": "♗", "knight": "♘", "pawn": "♙"},
    "black": {"king": "♚", "queen": "♛", "bishop": "♝", "knight": "♞", "pawn": "♟"},
}

HEX_BASE_COLORS = [(210, 200, 186), (180, 168, 152), (148, 134, 116)]
HEX_BORDER      = (90, 80, 68)
BOARD_BG        = (232, 225, 214)
WHITE_PIECE_FG  = (248, 242, 226)
WHITE_PIECE_OL  = (42,  35,  24)
BLACK_PIECE_FG  = (28,  22,  16)
BLACK_PIECE_OL  = (205, 196, 178)

pygame.init()
surface     = pygame.Surface((WIDTH, HEIGHT))
label_font  = pygame.font.Font(FONT_PATH, 10)
_pfcache    = {}
render_lock = threading.Lock()
rooms       = {}
rooms_lock  = threading.Lock()


# ---------------------------------------------------------------------------
# Room helpers
# ---------------------------------------------------------------------------

def make_room(time_limit=300, ai=False, ai_difficulty="medium"):
    now = time.time()
    tl  = float(time_limit) if time_limit > 0 else None
    return {
        "game":              Game(size=BOARD_SIZE),
        "selected":          None,
        "legal_moves":       [],
        "winner":            None,
        "win_reason":        None,
        "pending_promotion": None,
        "last_move":         None,
        "history":           [],
        "white_time":        tl,
        "black_time":        tl,
        "init_time":         tl,
        "clock_since":       now if tl else None,
        "ai":                ai,
        "ai_color":          "black" if ai else None,
        "ai_difficulty":     ai_difficulty,
        "ai_thinking":       False,
        "last_event":        None,
        "event_seq":         0,
        "lock":              threading.Lock(),
        "created":           now,
    }


def get_room(room_id):
    with rooms_lock:
        return rooms.get(room_id)


def get_time_remaining(room, color):
    key  = "white_time" if color == "white" else "black_time"
    base = room[key]
    if base is None:
        return None
    since = room["clock_since"]
    if since is not None and room["game"].turn == color and not room["winner"]:
        return max(0.0, base - (time.time() - since))
    return base


def deduct_clock(room):
    since = room["clock_since"]
    if since is None:
        return
    elapsed = time.time() - since
    key = "white_time" if room["game"].turn == "white" else "black_time"
    if room[key] is not None:
        room[key] = max(0.0, room[key] - elapsed)
    room["clock_since"] = time.time()


def check_timer_expiry(room):
    if room["winner"] or room["pending_promotion"]:
        return
    since = room["clock_since"]
    if since is None:
        return
    turn = room["game"].turn
    key  = "white_time" if turn == "white" else "black_time"
    if room[key] is not None and room[key] - (time.time() - since) <= 0:
        room["winner"]      = "black" if turn == "white" else "white"
        room["win_reason"]  = "timeout"
        room["clock_since"] = None
        room["last_event"]  = "timeout"
        room["event_seq"]  += 1


def check_game_over(game):
    cur = game.turn
    if game.is_checkmate(cur):
        return ("checkmate", "black" if cur == "white" else "white")
    if game.is_stalemate(cur):
        return ("stalemate", None)
    if game.is_draw():
        return ("draw", None)
    return None


def apply_result(room, result):
    if result:
        kind, w = result
        room["winner"]     = w if kind == "checkmate" else "draw"
        room["win_reason"] = kind


def compute_event(cap_name, game, is_over):
    """Determine sound event after a move was made (game.turn = next player now)."""
    if is_over:
        return "checkmate"
    if game.is_in_check(game.turn):
        return "check"
    if cap_name:
        return "capture"
    return "move"


def record_move(room, color, piece_name, src, dst, captured=None, promo_to=None):
    game = room["game"]
    room["history"].append({
        "color":      color,
        "sym":        PIECE_SYMBOLS[color][piece_name],
        "from_label": game.to_label(*src),
        "to_label":   game.to_label(*dst),
        "captured":   captured,
        "promo_to":   promo_to,
    })


# ---------------------------------------------------------------------------
# Minimax AI engine
# ---------------------------------------------------------------------------

def apply_move_search(game, src, dst):
    """Apply move in-place for search. Returns undo token."""
    piece       = game.board[src]
    captured    = game.board[dst]
    prev_moved  = piece.has_moved
    prev_name   = piece.name
    prev_turn   = game.turn
    prev_ep     = game.en_passant_target
    prev_half   = game.halfmove_clock

    # En passant capture during search, mirroring Game.move().
    ep_pos, ep_captured = None, None
    if piece.name == "pawn" and dst == game.en_passant_target and captured is None:
        forward  = (0, -1) if piece.owner == "white" else (0, 1)
        ep_pos   = (dst[0] - forward[0], dst[1] - forward[1])
        ep_captured = game.board.get(ep_pos)
        game.board[ep_pos] = None

    game.board[dst] = piece
    game.board[src] = None
    piece.has_moved = True
    # Auto-promote to queen during search
    if piece.name == "pawn" and game.is_promotion_square(dst, piece.owner):
        piece.name = "queen"

    new_ep = None
    if prev_name == "pawn" and src[0] == dst[0] and abs(src[1] - dst[1]) == 2:
        new_ep = ((src[0] + dst[0]) // 2, (src[1] + dst[1]) // 2)
    game.en_passant_target = new_ep

    if prev_name == "pawn" or captured is not None or ep_captured is not None:
        game.halfmove_clock = 0
    else:
        game.halfmove_clock = prev_half + 1

    game.turn = "black" if game.turn == "white" else "white"
    return (src, dst, piece, captured, prev_moved, prev_name, prev_turn,
            prev_ep, prev_half, ep_pos, ep_captured)


def undo_move_search(game, tok):
    (src, dst, piece, captured, prev_moved, prev_name, prev_turn,
     prev_ep, prev_half, ep_pos, ep_captured) = tok
    game.board[src] = piece
    game.board[dst] = captured
    piece.has_moved = prev_moved
    piece.name      = prev_name
    game.turn       = prev_turn
    game.en_passant_target = prev_ep
    game.halfmove_clock    = prev_half
    if ep_pos is not None:
        game.board[ep_pos] = ep_captured


def evaluate_board(game, ai_color):
    """Material + positional heuristic from AI's perspective."""
    MAT   = {"queen": 9.0, "bishop": 3.25, "knight": 3.0, "pawn": 1.0, "king": 0.0}
    score = 0.0
    for (q, r), piece in game.board.items():
        if piece is None:
            continue
        val = MAT.get(piece.name, 0.0)
        # Pawn advancement bonus (0.15 per rank advanced)
        if piece.name == "pawn":
            adv = (-r) if piece.owner == "white" else r
            val += 0.15 * adv
        elif piece.name in ("knight", "bishop", "queen"):
            # Central squares have more reach on a hex board.
            dist = (abs(q) + abs(r) + abs(q + r)) / 2
            val += max(0.0, 0.08 * (game.size - dist))
        # Cheap mobility proxy (pseudo-moves, no check simulation).
        val += 0.02 * len(game._pseudo_moves((q, r)))
        score += val if piece.owner == ai_color else -val

    opponent = "black" if ai_color == "white" else "white"
    if game.is_in_check(ai_color):
        score -= 0.5
    if game.is_in_check(opponent):
        score += 0.5
    return score


def _collect_moves(game):
    """Collect all legal moves for the current player, captures first."""
    moves = []
    for pos, piece in list(game.board.items()):
        if piece and piece.owner == game.turn:
            for end in game.legal_moves(pos):
                cap = game.board.get(end)
                cap_val = PIECE_VALUES.get(cap.name, 0) if cap else 0
                moves.append((cap_val, pos, end))
    moves.sort(key=lambda x: -x[0])   # captures first for better pruning
    return moves


class _TimeUp(Exception):
    pass


def minimax_search(game, depth, alpha, beta, maximizing, ai_color, deadline=None):
    if deadline is not None and time.time() > deadline:
        raise _TimeUp()

    if depth == 0:
        return evaluate_board(game, ai_color), None

    moves = _collect_moves(game)
    if not moves:
        cur = game.turn
        if game.is_in_check(cur):
            return (-9999 if cur == ai_color else 9999), None
        return 0.0, None   # stalemate

    best_move  = None
    if maximizing:
        best_val = -float("inf")
        for _, src, dst in moves:
            tok = apply_move_search(game, src, dst)
            try:
                val, _ = minimax_search(game, depth - 1, alpha, beta, False, ai_color, deadline)
            finally:
                undo_move_search(game, tok)
            if val > best_val:
                best_val, best_move = val, (src, dst)
            alpha = max(alpha, val)
            if beta <= alpha:
                break
    else:
        best_val = float("inf")
        for _, src, dst in moves:
            tok = apply_move_search(game, src, dst)
            try:
                val, _ = minimax_search(game, depth - 1, alpha, beta, True, ai_color, deadline)
            finally:
                undo_move_search(game, tok)
            if val < best_val:
                best_val, best_move = val, (src, dst)
            beta = min(beta, val)
            if beta <= alpha:
                break

    return best_val, best_move


def find_best_move(game_copy, ai_color, difficulty):
    """Return (src, dst) for the AI, or (None, None) if no moves."""
    moves = _collect_moves(game_copy)
    if not moves:
        return None, None

    if difficulty == "easy":
        _, src, dst = random.choice(moves)
        return src, dst

    if difficulty == "medium":
        # Depth-1: pick best immediate move by material eval
        best_val = -float("inf")
        candidates = []
        for _, src, dst in moves:
            tok = apply_move_search(game_copy, src, dst)
            val = evaluate_board(game_copy, ai_color)
            undo_move_search(game_copy, tok)
            if val > best_val:
                best_val = val
                candidates = [(src, dst)]
            elif val == best_val:
                candidates.append((src, dst))
        return random.choice(candidates)

    # "hard": iterative-deepening minimax with alpha-beta, bounded by a
    # time budget rather than a fixed depth so it scales with whatever
    # the branching factor of the current position happens to be.
    deadline = time.time() + 2.5
    best = None
    depth = 1
    while depth <= 5:
        try:
            _, mv = minimax_search(game_copy, depth, -float("inf"), float("inf"), True, ai_color, deadline)
        except _TimeUp:
            break
        if mv is not None:
            best = mv
        if time.time() >= deadline:
            break
        depth += 1
    if best is None:
        _, src, dst = random.choice(moves)
        return src, dst
    return best


# ---------------------------------------------------------------------------
# AI trigger (runs outside room lock to avoid blocking frame renders)
# ---------------------------------------------------------------------------

def trigger_ai_move(room_id):
    room = get_room(room_id)
    if not room:
        return

    with room["lock"]:
        room["ai_thinking"] = True

    try:
        time.sleep(0.45)

        # Stage 1: snapshot game state under lock
        with room["lock"]:
            check_timer_expiry(room)
            if room["winner"] or room["pending_promotion"]:
                return
            ai_color   = room.get("ai_color", "black")
            difficulty = room.get("ai_difficulty", "medium")
            if room["game"].turn != ai_color:
                return
            game_copy = copy.deepcopy(room["game"])

        # Stage 2: find best move OUTSIDE lock (CPU-intensive)
        src, dst = find_best_move(game_copy, ai_color, difficulty)
        if src is None:
            return

        # Stage 3: apply the chosen move under lock
        with room["lock"]:
            if room["winner"] or room["pending_promotion"]:
                return
            game = room["game"]
            if game.turn != ai_color:
                return

            moving_piece = game.board.get(src)
            if moving_piece is None:
                return
            captured  = game.board.get(dst)
            cap_name  = captured.name if captured else None
            is_promo  = (moving_piece.name == "pawn"
                         and game.is_promotion_square(dst, moving_piece.owner))

            deduct_clock(room)

            if is_promo:
                game.board[dst] = moving_piece
                game.board[src] = None
                moving_piece.has_moved   = True
                game.board[dst].name     = "queen"
                game.en_passant_target   = None
                game.halfmove_clock      = 0
                game.turn = "black" if game.turn == "white" else "white"
                game.record_position()
                record_move(room, ai_color, "pawn", src, dst, cap_name, promo_to="queen")
            else:
                game.move(src, dst)
                record_move(room, ai_color, moving_piece.name, src, dst, cap_name)

            room["last_move"] = {"from": src, "to": dst}
            apply_result(room, check_game_over(game))
            event = compute_event(cap_name, game, room["winner"] is not None)
            room["last_event"] = event
            room["event_seq"] += 1
    finally:
        with room["lock"]:
            room["ai_thinking"] = False


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def get_piece_font(size):
    if size not in _pfcache:
        _pfcache[size] = pygame.font.Font(FONT_PATH, size)
    return _pfcache[size]


def draw_hex(x, y, size, fill, border=HEX_BORDER):
    pts = [(x + size * math.cos(math.radians(60 * i)),
            y + size * math.sin(math.radians(60 * i))) for i in range(6)]
    pygame.draw.polygon(surface, fill,   pts)
    pygame.draw.polygon(surface, border, pts, 1)


def draw_piece(sym, fg, ol, font, cx, cy):
    ol_s = font.render(sym, True, ol)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        surface.blit(ol_s, ol_s.get_rect(center=(cx + dx, cy + dy)))
    surface.blit(font.render(sym, True, fg),
                 font.render(sym, True, fg).get_rect(center=(cx, cy)))


def fmt_time(secs):
    if secs is None:
        return "--:--"
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


def _clock_bg(rem, init):
    if rem is None or init is None or init == 0:
        return (80, 80, 80)
    f = rem / init
    if f > 0.5:  return (34, 158, 34)
    if f > 0.2:  return (205, 155, 18)
    return (195, 42, 42)


def draw_clock_badge(x, y, w, h, label, time_str, is_active, rem, init, cfont, sfont):
    bg  = _clock_bg(rem, init) if is_active else (44, 44, 44)
    bdr = (0, 0, 0) if is_active else (88, 88, 88)
    fg  = (255, 255, 255) if is_active else (110, 110, 110)
    pygame.draw.rect(surface, bg,  (x, y, w, h), border_radius=8)
    pygame.draw.rect(surface, bdr, (x, y, w, h), 2, border_radius=8)
    surface.blit(sfont.render(label, True, fg),
                 sfont.render(label, True, fg).get_rect(midtop=(x + w // 2, y + 3)))
    surface.blit(cfont.render(time_str, True, fg),
                 cfont.render(time_str, True, fg).get_rect(midbottom=(x + w // 2, y + h - 3)))


def render_room(room):
    game      = room["game"]
    selected  = room["selected"]
    legal_set = set(room["legal_moves"])
    lm        = room.get("last_move") or {}
    last_from = lm.get("from")
    last_to   = lm.get("to")

    in_check_now = not room.get("winner") and game.is_in_check(game.turn)
    king_pos = None
    if in_check_now:
        for pos, piece in game.board.items():
            if piece and piece.name == "king" and piece.owner == game.turn:
                king_pos = pos
                break

    surface.fill(BOARD_BG)

    for (q, r), piece in game.board.items():
        x, y, ts = game.to_pixel(q, r, WIDTH, HEIGHT, zoom=ZOOM)
        hr = int(ts * DRAW_SCALE)

        if selected == (q, r):
            color = (245, 200, 28)
        elif (q, r) in legal_set:
            color = (88, 182, 106)
        elif (q, r) == king_pos:
            color = (196, 46, 46)
        elif (q, r) == last_to:
            color = (80, 138, 205)
        elif (q, r) == last_from:
            color = (138, 182, 225)
        else:
            color = HEX_BASE_COLORS[(q - r) % 3]

        draw_hex(x, y, hr, color)

        if piece:
            pf  = get_piece_font(int(hr * 1.5))
            sym = PIECE_SYMBOLS[piece.owner][piece.name]
            if piece.owner == "white":
                draw_piece(sym, WHITE_PIECE_FG, WHITE_PIECE_OL, pf, x, y)
            else:
                draw_piece(sym, BLACK_PIECE_FG, BLACK_PIECE_OL, pf, x, y)

        lt = label_font.render(game.to_label(q, r), False, (100, 88, 72))
        surface.blit(lt, lt.get_rect(center=(x, y + hr * 0.65)))

    winner = room.get("winner")
    cfont  = pygame.font.Font(FONT_PATH, 20)
    sfont  = pygame.font.Font(FONT_PATH, 10)
    cw, ch = 105, 44

    if winner:
        ov = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        ov.fill((0, 0, 0, 170))
        surface.blit(ov, (0, 0))
        big = pygame.font.Font(FONT_PATH, 38)
        sub = pygame.font.Font(FONT_PATH, 19)
        if winner == "draw" and room.get("win_reason") == "draw":
            title = big.render("Draw — repetition / 50-move rule", True, (200, 200, 255))
        elif winner == "draw":
            title = big.render("Stalemate — Draw!", True, (200, 200, 255))
        elif room.get("win_reason") == "timeout":
            loser = "black" if winner == "white" else "white"
            sym   = "♔" if winner == "white" else "♚"
            title = big.render(f"{sym}  {loser.capitalize()} ran out of time!", True, (255, 218, 48))
        else:
            sym   = "♔" if winner == "white" else "♚"
            title = big.render(f"{sym}  {winner.capitalize()} wins by checkmate!", True, (255, 218, 48))
        hint = sub.render("Press Reset Game to play again", True, (195, 195, 195))
        surface.blit(title, title.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 28)))
        surface.blit(hint,  hint.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 28)))
    else:
        in_check = game.is_in_check(game.turn)
        bg  = (168, 26, 26) if in_check else ((238, 235, 228) if game.turn == "white" else (26, 26, 26))
        fg  = (255, 255, 255) if (in_check or game.turn == "black") else (0, 0, 0)
        tw  = "White" if game.turn == "white" else "Black"
        lbl = f"{tw} is in CHECK!" if in_check else f"{tw}'s turn"
        lf  = pygame.font.Font(FONT_PATH, 16)
        bw  = 212 if in_check else 152
        pygame.draw.rect(surface, bg,         (10, 10, bw, 34), border_radius=8)
        pygame.draw.rect(surface, HEX_BORDER, (10, 10, bw, 34), 2, border_radius=8)
        surface.blit(lf.render(lbl, True, fg),
                     lf.render(lbl, True, fg).get_rect(center=(10 + bw // 2, 27)))

        if room.get("ai_thinking"):
            tf  = pygame.font.Font(FONT_PATH, 13)
            txt = "AI is thinking…"
            tw2 = tf.size(txt)[0]
            bx, by, bwid, bhei = 10, 50, tw2 + 20, 26
            pygame.draw.rect(surface, (44, 44, 44),   (bx, by, bwid, bhei), border_radius=7)
            pygame.draw.rect(surface, HEX_BORDER,     (bx, by, bwid, bhei), 2, border_radius=7)
            surface.blit(tf.render(txt, True, (150, 190, 230)),
                         tf.render(txt, True, (150, 190, 230)).get_rect(center=(bx + bwid // 2, by + bhei // 2)))

        init = room.get("init_time")
        if init is not None:
            cx    = WIDTH - cw - 10
            b_rem = get_time_remaining(room, "black")
            w_rem = get_time_remaining(room, "white")
            draw_clock_badge(cx, 10,               cw, ch, "BLACK",
                             fmt_time(b_rem), game.turn == "black", b_rem, init, cfont, sfont)
            draw_clock_badge(cx, HEIGHT - ch - 10, cw, ch, "WHITE",
                             fmt_time(w_rem), game.turn == "white", w_rem, init, cfont, sfont)


def get_frame_bytes(room):
    with render_lock:
        with room["lock"]:
            check_timer_expiry(room)
            render_room(room)
        buf = io.BytesIO()
        pygame.image.save(surface, buf, "frame.png")
        buf.seek(0)
        return buf.read()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template_string(LANDING_HTML)


@app.route('/new', methods=['POST'])
def new_game():
    room_id = uuid.uuid4().hex[:10]
    try:
        time_limit = int(request.form.get('time_limit', 300))
    except (ValueError, TypeError):
        time_limit = 300
    ai         = request.form.get('ai', '0') == '1'
    difficulty = request.form.get('difficulty', 'medium')
    if difficulty not in ('easy', 'medium', 'hard'):
        difficulty = 'medium'
    with rooms_lock:
        rooms[room_id] = make_room(time_limit=time_limit, ai=ai, ai_difficulty=difficulty)
    return redirect(url_for('game_page', room_id=room_id))


@app.route('/game/<room_id>')
def game_page(room_id):
    room = get_room(room_id)
    if room is None:
        return "Game not found. <a href='/'>Create a new game</a>", 404
    first_frame = base64.b64encode(get_frame_bytes(room)).decode('utf-8')
    return render_template_string(GAME_HTML, room_id=room_id,
                                  first_frame=first_frame,
                                  ai_mode=room.get("ai", False),
                                  ai_difficulty=room.get("ai_difficulty", "medium"))


@app.route('/frame/<room_id>')
def frame(room_id):
    room = get_room(room_id)
    if room is None:
        return '', 404
    return Response(get_frame_bytes(room), mimetype='image/png',
                    headers={'Cache-Control': 'no-store'})


@app.route('/history/<room_id>')
def history(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify([])
    with room["lock"]:
        return jsonify(list(room["history"]))


@app.route('/state/<room_id>')
def state(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    with room["lock"]:
        return jsonify({
            'pending_promotion': room["pending_promotion"] is not None,
            'turn':              room["game"].turn,
            'event_seq':         room["event_seq"],
            'last_event':        room["last_event"],
        })


@app.route('/click/<room_id>', methods=['POST'])
def click(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404

    data  = request.json
    img_w = data.get('imgW', WIDTH)
    img_h = data.get('imgH', HEIGHT)
    mx    = int(data['x'] * WIDTH  / img_w)
    my    = int(data['y'] * HEIGHT / img_h)

    ai_triggered = False
    click_event  = None

    with room["lock"]:
        check_timer_expiry(room)
        if room["winner"] or room["pending_promotion"]:
            return jsonify({'ok': True})

        game = room["game"]
        if room["ai"] and game.turn == room["ai_color"]:
            return jsonify({'ok': True})

        q, r = game.from_pixel(mx, my, WIDTH, HEIGHT, zoom=ZOOM)
        if (q, r) not in game.board:
            room["selected"] = None
            room["legal_moves"] = []
            return jsonify({'ok': True})

        piece = game.board.get((q, r))

        if room["selected"] is None:
            if piece and piece.owner == game.turn:
                room["selected"]    = (q, r)
                room["legal_moves"] = game.legal_moves((q, r))
                click_event = "select" if room["legal_moves"] else None
        else:
            if (q, r) in room["legal_moves"]:
                src          = room["selected"]
                moving_piece = game.board[src]
                captured     = game.board.get((q, r))
                cap_name     = captured.name if captured else None
                color        = game.turn
                is_promo     = (moving_piece.name == "pawn"
                                and game.is_promotion_square((q, r), moving_piece.owner))
                if is_promo:
                    deduct_clock(room)
                    game.board[(q, r)]       = moving_piece
                    game.board[src]          = None
                    moving_piece.has_moved   = True
                    game.en_passant_target   = None
                    game.halfmove_clock      = 0
                    room["pending_promotion"] = (q, r)
                    room["selected"] = None
                    room["legal_moves"] = []
                    room["last_move"]        = {"from": src, "to": (q, r)}
                    record_move(room, color, "pawn", src, (q, r), cap_name)
                    click_event = "move"
                else:
                    deduct_clock(room)
                    game.move(src, (q, r))
                    record_move(room, color, moving_piece.name, src, (q, r), cap_name)
                    room["last_move"] = {"from": src, "to": (q, r)}
                    room["selected"] = None
                    room["legal_moves"] = []
                    apply_result(room, check_game_over(game))
                    click_event = compute_event(cap_name, game, room["winner"] is not None)
                    room["last_event"] = click_event
                    room["event_seq"] += 1
                    if not room["winner"] and room["ai"] and game.turn == room["ai_color"]:
                        ai_triggered = True
            else:
                # Re-select another own piece or deselect
                if piece and piece.owner == game.turn:
                    room["selected"]    = (q, r)
                    room["legal_moves"] = game.legal_moves((q, r))
                    click_event = "select"
                else:
                    room["selected"]    = None
                    room["legal_moves"] = []

    if ai_triggered:
        threading.Thread(target=trigger_ai_move, args=(room_id,), daemon=True).start()

    return jsonify({'ok': True, 'event': click_event})


@app.route('/reset/<room_id>', methods=['POST'])
def reset(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    with room["lock"]:
        init = room["init_time"]
        room["game"]              = Game(size=BOARD_SIZE)
        room["selected"]          = None
        room["legal_moves"]       = []
        room["winner"]            = None
        room["win_reason"]        = None
        room["pending_promotion"] = None
        room["last_move"]         = None
        room["history"]           = []
        room["white_time"]        = init
        room["black_time"]        = init
        room["clock_since"]       = time.time() if init else None
        room["last_event"]        = None
        room["ai_thinking"]       = False
        room["event_seq"]        += 1
    return jsonify({'ok': True})


@app.route('/promote/<room_id>', methods=['POST'])
def promote(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    piece_name = request.json.get('piece', 'queen')
    if piece_name not in ('queen', 'bishop', 'knight'):
        return jsonify({'ok': False, 'error': 'invalid piece'}), 400

    ai_triggered = False

    with room["lock"]:
        pos = room["pending_promotion"]
        if pos is None:
            return jsonify({'ok': False, 'error': 'no promotion pending'}), 400
        game = room["game"]
        game.board[pos].name      = piece_name
        room["pending_promotion"] = None
        if room["history"]:
            room["history"][-1]["promo_to"] = piece_name
            room["history"][-1]["sym"]      = PIECE_SYMBOLS[game.turn][piece_name]
        deduct_clock(room)
        game.turn = "black" if game.turn == "white" else "white"
        game.record_position()
        apply_result(room, check_game_over(game))
        event = compute_event(None, game, room["winner"] is not None)
        room["last_event"] = event
        room["event_seq"] += 1
        if not room["winner"] and room["ai"] and game.turn == room["ai_color"]:
            ai_triggered = True

    if ai_triggered:
        threading.Thread(target=trigger_ai_move, args=(room_id,), daemon=True).start()

    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

LANDING_HTML = r'''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hex Chess</title>
  <style>
    *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
    body {
      background: radial-gradient(ellipse at 50% 30%, #1e2540 0%, #0f1220 100%);
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      min-height:100vh; font-family:'Segoe UI', system-ui, sans-serif; color:#eee;
    }
    .card {
      background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.1);
      border-radius:20px; padding:40px 48px 36px; width:440px;
      display:flex; flex-direction:column; align-items:center;
      box-shadow:0 24px 60px rgba(0,0,0,0.5);
    }
    h1 {
      font-size:2.4rem; letter-spacing:6px;
      background:linear-gradient(135deg, #e8dfc8 30%, #a89060 100%);
      -webkit-background-clip:text; -webkit-text-fill-color:transparent;
      margin-bottom:6px;
    }
    .subtitle { color:#7a8090; font-size:0.88rem; margin-bottom:28px; letter-spacing:0.5px; }

    .mode-row { display:flex; gap:12px; width:100%; margin-bottom:20px; }
    .mode-btn {
      flex:1; padding:16px 10px; border:2px solid rgba(255,255,255,0.12);
      background:rgba(255,255,255,0.04); border-radius:12px; color:#aaa;
      cursor:pointer; transition:all 0.18s; text-align:center; user-select:none;
    }
    .mode-btn .icon { font-size:1.8rem; display:block; margin-bottom:6px; }
    .mode-btn .name { font-size:0.82rem; font-weight:700; letter-spacing:1.5px; display:block; }
    .mode-btn .desc { font-size:0.72rem; color:#666; margin-top:4px; display:block; }
    .mode-btn:hover  { border-color:rgba(255,255,255,0.3); color:#ddd; }
    .mode-btn.active { border-color:#4a90d9; background:rgba(74,144,217,0.18); color:#fff; }
    .mode-btn.active .desc { color:#89b8e8; }

    .field { width:100%; margin-bottom:18px; }
    .field label { display:block; font-size:0.75rem; letter-spacing:1.5px;
                   color:#6a7080; margin-bottom:8px; }
    .field select {
      width:100%; padding:11px 14px; background:rgba(255,255,255,0.06);
      color:#eee; border:1px solid rgba(255,255,255,0.14); border-radius:8px;
      font-size:0.95rem; cursor:pointer; outline:none; appearance:none;
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%23888' d='M6 8L0 0h12z'/%3E%3C/svg%3E");
      background-repeat:no-repeat; background-position:right 14px center;
    }
    .field select option { background:#1a2030; }

    #ai-fields { width:100%; }
    .ai-note {
      font-size:0.78rem; color:#6a9fc8; margin-bottom:18px; width:100%;
      padding:8px 12px; background:rgba(74,144,217,0.1);
      border-radius:6px; border-left:3px solid #4a90d9;
    }

    .create-btn {
      width:100%; padding:14px; font-size:1rem; font-weight:700; letter-spacing:2px;
      background:linear-gradient(135deg,#3a80c8,#2060a8); color:#fff; border:none;
      border-radius:10px; cursor:pointer; transition:all 0.18s;
      box-shadow:0 4px 18px rgba(42,100,180,0.45);
    }
    .create-btn:hover {
      background:linear-gradient(135deg,#4a90d8,#3070b8);
      box-shadow:0 6px 24px rgba(42,100,180,0.6); transform:translateY(-1px);
    }
  </style>
</head>
<body>
  <form method="POST" action="/new" id="form">
    <input type="hidden" name="ai" id="ai_hidden" value="0">
    <div class="card">
      <h1>HEX CHESS</h1>
      <p class="subtitle">a Loridanshof original</p>

      <div class="mode-row">
        <div class="mode-btn active" id="btn-multi" onclick="setMode(0)">
          <span class="icon">👥</span>
          <span class="name">2 PLAYERS</span>
          <span class="desc">Share the link<br>with a friend</span>
        </div>
        <div class="mode-btn" id="btn-ai" onclick="setMode(1)">
          <span class="icon">🤖</span>
          <span class="name">VS AI</span>
          <span class="desc">You play White<br>AI plays Black</span>
        </div>
      </div>

      <div id="ai-fields" style="display:none;">
        <div class="field">
          <label for="difficulty">AI DIFFICULTY</label>
          <select name="difficulty" id="difficulty">
            <option value="easy">Plays randomly</option>
            <option value="medium" selected>Greedy, prefers captures</option>
            <option value="hard">Searches several moves ahead</option>
          </select>
        </div>
      </div>

      <div class="field">
        <label for="time_limit">TIME PER PLAYER</label>
        <select name="time_limit" id="time_limit">
          <option value="60">1 minute</option>
          <option value="180">3 minutes</option>
          <option value="300" selected>5 minutes</option>
          <option value="600">10 minutes</option>
          <option value="0">No limit</option>
        </select>
      </div>

      <button type="submit" class="create-btn">CREATE GAME</button>
    </div>
  </form>
  <script>
    function setMode(ai) {
      document.getElementById('ai_hidden').value = ai ? '1' : '0';
      document.getElementById('btn-multi').classList.toggle('active', !ai);
      document.getElementById('btn-ai').classList.toggle('active',  !!ai);
      document.getElementById('ai-fields').style.display = ai ? '' : 'none';
    }
  </script>
</body>
</html>'''


GAME_HTML = r'''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hex Chess</title>
  <style>
    *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
    body {
      background:radial-gradient(ellipse at 50% 30%, #1e2540 0%, #0f1220 100%);
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      min-height:100vh; padding:16px; font-family:'Segoe UI', system-ui, sans-serif;
    }
    h1 { color:#e0d8c8; font-size:1.25rem; letter-spacing:4px; margin-bottom:8px; }
    #share-box { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
    #share-url { background:rgba(255,255,255,0.06); color:#8ac0e0;
                 border:1px solid rgba(74,144,217,0.4); border-radius:6px;
                 padding:6px 12px; font-size:0.78rem; font-family:monospace;
                 width:300px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
    #copy-btn { padding:6px 14px; background:#2a70b8; color:#fff; border:none;
                border-radius:6px; cursor:pointer; font-size:0.78rem; transition:background 0.18s; }
    #copy-btn:hover { background:#3a80c8; }
    #ai-badge { color:#6aacda; font-size:0.78rem; letter-spacing:1px; margin-bottom:6px; }

    #game-area { display:flex; align-items:flex-start; gap:14px; }
    #game-wrap { position:relative; display:inline-block; line-height:0; }
    #game { cursor:pointer; border-radius:10px; display:block;
            box-shadow:0 8px 40px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.07);
            max-width:100%; max-height:74vh; }
    #overlay { position:absolute; top:0; left:0; pointer-events:none; border-radius:10px; }

    #history-panel {
      width:200px; min-width:200px; background:rgba(255,255,255,0.04);
      border:1px solid rgba(255,255,255,0.09); border-radius:10px;
      display:flex; flex-direction:column; max-height:74vh; overflow:hidden;
    }
    #history-title { padding:10px 14px 8px; font-size:0.72rem; letter-spacing:2px;
                     color:#5a6878; border-bottom:1px solid rgba(255,255,255,0.07); flex-shrink:0; }
    #history-list { overflow-y:auto; flex:1; padding:6px 0;
                    scrollbar-width:thin; scrollbar-color:#334 transparent; }
    #history-list::-webkit-scrollbar { width:4px; }
    #history-list::-webkit-scrollbar-thumb { background:#334; border-radius:2px; }
    .move-pair { padding:3px 14px; }
    .move-pair:nth-child(even) { background:rgba(255,255,255,0.02); }
    .pair-num { font-size:0.68rem; color:#3a4858; margin-bottom:1px; }
    .move-entry { display:flex; align-items:center; gap:5px; padding:2px 0; font-size:0.8rem; }
    .move-entry .sym { font-size:1rem; line-height:1; }
    .move-entry .coords { color:#90a0b0; letter-spacing:0.3px; }
    .move-entry.white-entry .sym { color:#e0d8c8; }
    .move-entry.black-entry .sym { color:#9098a8; }
    .capture-dot { width:5px; height:5px; border-radius:50%; background:#c05050; flex-shrink:0; }
    .promo-badge { font-size:0.6rem; color:#d4a020; background:rgba(212,160,32,0.15);
                   border-radius:3px; padding:1px 4px; }
    .history-empty { color:#3a4858; font-size:0.78rem; text-align:center; padding:20px 14px; }

    #bottom-row { display:flex; align-items:center; justify-content:center; gap:16px; margin-top:10px; }
    #reset-btn { padding:8px 24px; background:#a82828; color:#fff; border:none;
                 border-radius:8px; font-size:0.88rem; cursor:pointer;
                 letter-spacing:1px; transition:background 0.18s; }
    #reset-btn:hover { background:#c03030; }

    #promo-overlay { display:none; position:fixed; inset:0;
                     background:rgba(0,0,0,0.78); z-index:100;
                     align-items:center; justify-content:center; }
    #promo-overlay.active { display:flex; }
    #promo-box { background:#1a2338; border:2px solid #3a70b8; border-radius:16px;
                 padding:30px 38px; text-align:center; color:#eee;
                 box-shadow:0 20px 60px rgba(0,0,0,0.6); }
    #promo-box h2 { font-size:1.2rem; letter-spacing:3px; margin-bottom:6px; }
    #promo-box p  { color:#7a90a8; margin-bottom:22px; font-size:0.88rem; }
    .promo-btn { padding:12px 20px; margin:0 6px; font-size:1.7rem;
                 background:rgba(58,112,184,0.3); color:#fff; border:2px solid #3a70b8;
                 border-radius:10px; cursor:pointer; transition:all 0.15s; line-height:1; }
    .promo-btn:hover { background:rgba(58,112,184,0.7); transform:scale(1.1); }
    .promo-label { display:block; font-size:0.65rem; margin-top:4px;
                   letter-spacing:1px; color:#89aacf; }
  </style>
</head>
<body>
  <h1>HEX CHESS</h1>
  {% if ai_mode %}
  <div id="ai-badge">⚔ VS AI — you play White
    {% if ai_difficulty == 'easy' %}· Easy{% elif ai_difficulty == 'hard' %}· Hard{% else %}· Medium{% endif %}
  </div>
  {% endif %}
  <div id="share-box">
    <span id="share-url"></span>
    <button id="copy-btn">Copy Link</button>
  </div>

  <div id="game-area">
    <div id="game-wrap">
      <img id="game" src="data:image/png;base64,{{ first_frame }}" draggable="false">
      <canvas id="overlay"></canvas>
    </div>
    <div id="history-panel">
      <div id="history-title">MOVE HISTORY</div>
      <div id="history-list">
        <div class="history-empty" id="history-empty">No moves yet</div>
      </div>
    </div>
  </div>

  <div id="bottom-row">
    <button id="reset-btn">Reset Game</button>
  </div>

  <div id="promo-overlay">
    <div id="promo-box">
      <h2>PAWN PROMOTION</h2>
      <p>Choose a piece to promote to:</p>
      <div id="promo-buttons"></div>
    </div>
  </div>

  <script>
    const ROOM = "{{ room_id }}";
    document.getElementById('share-url').textContent = window.location.href;
    document.getElementById('copy-btn').addEventListener('click', function() {
      navigator.clipboard.writeText(window.location.href).then(() => {
        this.textContent = 'Copied!';
        setTimeout(() => this.textContent = 'Copy Link', 1500);
      });
    });

    // ================================================================
    // SOUND ENGINE (Web Audio API — fully procedural, no files needed)
    // ================================================================
    const AC = window.AudioContext || window.webkitAudioContext;
    let audioCtx = null;
    function ensureAudio() {
      if (!audioCtx && AC) { try { audioCtx = new AC(); } catch(e){} }
      return audioCtx;
    }
    function tone(freq, type, dur, vol, freqEnd) {
      const ctx = ensureAudio(); if (!ctx) return;
      const osc = ctx.createOscillator(), g = ctx.createGain();
      osc.connect(g); g.connect(ctx.destination);
      osc.type = type; osc.frequency.setValueAtTime(freq, ctx.currentTime);
      if (freqEnd) osc.frequency.exponentialRampToValueAtTime(freqEnd, ctx.currentTime + dur);
      g.gain.setValueAtTime(vol, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
      osc.start(ctx.currentTime); osc.stop(ctx.currentTime + dur);
    }
    function playSelect()    { tone(740, 'sine',     0.07, 0.12); }
    function playMove()      { tone(240, 'triangle', 0.13, 0.22, 90); }
    function playCapture()   {
      tone(520, 'sawtooth', 0.09, 0.20);
      setTimeout(() => tone(380, 'sawtooth', 0.12, 0.14), 70);
    }
    function playCheck()     {
      tone(600, 'square', 0.06, 0.13);
      setTimeout(() => tone(750, 'square', 0.06, 0.13), 110);
      setTimeout(() => tone(900, 'square', 0.10, 0.13), 220);
    }
    function playCheckmate() {
      tone(880, 'square', 0.14, 0.20);
      setTimeout(() => tone(698, 'square', 0.14, 0.18), 200);
      setTimeout(() => tone(523, 'square', 0.18, 0.16), 420);
      setTimeout(() => tone(392, 'square', 0.24, 0.14), 650);
    }
    function playTimeout()   { tone(220, 'sawtooth', 0.5, 0.2, 110); }
    function playSound(evt) {
      if (!evt) return;
      if (evt === 'select')    playSelect();
      else if (evt === 'move')      playMove();
      else if (evt === 'capture')   playCapture();
      else if (evt === 'check')     playCheck();
      else if (evt === 'checkmate') playCheckmate();
      else if (evt === 'timeout')   playTimeout();
    }

    // ================================================================
    // HOVER OVERLAY
    // ================================================================
    const img     = document.getElementById('game');
    const overlay = document.getElementById('overlay');
    const ctx     = overlay.getContext('2d');
    const ZOOM  = 0.6, BSIZE = 4, IMG_W = 700, IMG_H = 580;

    function hexToPixel(q, r) {
      const base = Math.floor(Math.min(IMG_W, IMG_H) / (2*BSIZE+2));
      const ts = Math.floor(base * ZOOM);
      return { x: IMG_W/2 + ts*1.5*q, y: IMG_H/2 + ts*0.866*(2*r+q), ts };
    }
    function pixelToHex(px, py) {
      const base = Math.floor(Math.min(IMG_W, IMG_H) / (2*BSIZE+2));
      const ts = Math.floor(base * ZOOM);
      const x0 = px - IMG_W/2, y0 = py - IMG_H/2;
      return hexRound((2/3)*x0/ts, (y0/(ts*0.866) - (2/3)*x0/ts)/2);
    }
    function hexRound(q, r) {
      const s=-q-r; let rq=Math.round(q), rr=Math.round(r), rs=Math.round(s);
      const dq=Math.abs(rq-q), dr=Math.abs(rr-r), ds=Math.abs(rs-s);
      if(dq>dr&&dq>ds) rq=-rr-rs; else if(dr>ds) rr=-rq-rs;
      return {q:rq, r:rr};
    }
    function onBoard(q,r) { return Math.abs(q)<=BSIZE&&Math.abs(r)<=BSIZE&&Math.abs(q+r)<=BSIZE; }
    function drawHoverHex(cx, cy, radius) {
      ctx.beginPath();
      for(let i=0;i<6;i++){
        const a=Math.PI/180*60*i;
        i===0?ctx.moveTo(cx+radius*Math.cos(a),cy+radius*Math.sin(a))
             :ctx.lineTo(cx+radius*Math.cos(a),cy+radius*Math.sin(a));
      }
      ctx.closePath();
      ctx.fillStyle='rgba(255,255,255,0.18)';
      ctx.strokeStyle='rgba(255,255,255,0.5)';
      ctx.lineWidth=1.5; ctx.fill(); ctx.stroke();
    }
    function syncOverlay() { const r=img.getBoundingClientRect(); overlay.width=r.width; overlay.height=r.height; }
    img.addEventListener('mousemove', function(e) {
      syncOverlay();
      const rect=img.getBoundingClientRect(), sx=IMG_W/rect.width, sy=IMG_H/rect.height;
      const {q,r}=pixelToHex((e.clientX-rect.left)*sx,(e.clientY-rect.top)*sy);
      ctx.clearRect(0,0,overlay.width,overlay.height);
      if(!onBoard(q,r)) return;
      const h=hexToPixel(q,r); drawHoverHex(h.x/sx, h.y/sy, h.ts*0.95/sx);
    });
    img.addEventListener('mouseleave',()=>ctx.clearRect(0,0,overlay.width,overlay.height));

    // ================================================================
    // FRAME POLLING
    // ================================================================
    let tick=0, promoActive=false;
    function refresh() {
      const n=new Image();
      n.onload=()=>{ img.src=n.src; syncOverlay(); tick++; if(tick%6===0) checkState(); setTimeout(refresh,80); };
      n.onerror=()=>setTimeout(refresh,300);
      n.src='/frame/'+ROOM+'?t='+Date.now();
    }
    setTimeout(refresh,80);

    // ================================================================
    // STATE POLLING (for AI move sounds + promotion detection)
    // ================================================================
    let lastEventSeq = -1;
    async function checkState() {
      try {
        const d = await (await fetch('/state/'+ROOM+'?t='+Date.now())).json();
        // Play sound for AI moves (or any server-side event)
        if (d.event_seq !== lastEventSeq && lastEventSeq !== -1) {
          playSound(d.last_event);
        }
        lastEventSeq = d.event_seq;
        // Promotion dialog
        if (d.pending_promotion && !promoActive) {
          promoActive=true;
          const syms={white:{queen:'♕',bishop:'♗',knight:'♘'},black:{queen:'♛',bishop:'♝',knight:'♞'}};
          const cont=document.getElementById('promo-buttons'); cont.innerHTML='';
          [['queen','Queen'],['bishop','Bishop'],['knight','Knight']].forEach(([p,l])=>{
            const b=document.createElement('button'); b.className='promo-btn';
            b.innerHTML=syms[d.turn][p]+'<span class="promo-label">'+l+'</span>';
            b.onclick=()=>choosePromo(p); cont.appendChild(b);
          });
          document.getElementById('promo-overlay').classList.add('active');
        } else if (!d.pending_promotion && promoActive) {
          promoActive=false;
          document.getElementById('promo-overlay').classList.remove('active');
        }
      } catch(e) {}
    }
    // Poll more frequently to catch AI events promptly
    setInterval(checkState, 300);

    async function choosePromo(piece) {
      promoActive=false;
      document.getElementById('promo-overlay').classList.remove('active');
      await fetch('/promote/'+ROOM,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({piece})});
    }

    // ================================================================
    // HISTORY POLLING
    // ================================================================
    let lastHistLen=0;
    async function refreshHistory() {
      try {
        const data=await(await fetch('/history/'+ROOM+'?t='+Date.now())).json();
        if(data.length!==lastHistLen){ lastHistLen=data.length; renderHistory(data); }
      } catch(e){}
      setTimeout(refreshHistory,1200);
    }
    setTimeout(refreshHistory,500);

    function renderHistory(moves) {
      const list=document.getElementById('history-list');
      document.getElementById('history-empty').style.display=moves.length?'none':'';
      list.querySelectorAll('.move-pair').forEach(e=>e.remove());
      for(let i=0;i<moves.length;i+=2){
        const num=Math.floor(i/2)+1;
        const pair=document.createElement('div'); pair.className='move-pair';
        pair.innerHTML=`<div class="pair-num">${num}</div>`+moveHTML(moves[i])+(moves[i+1]?moveHTML(moves[i+1]):'');
        list.appendChild(pair);
      }
      list.scrollTop=list.scrollHeight;
    }
    function moveHTML(m) {
      const cls=m.color==='white'?'white-entry':'black-entry';
      const cap=m.captured?'<span class="capture-dot"></span>':'';
      const promo=m.promo_to?`<span class="promo-badge">=${m.promo_to[0].toUpperCase()}</span>`:'';
      return `<div class="move-entry ${cls}"><span class="sym">${m.sym}</span><span class="coords">${m.from_label}→${m.to_label}</span>${cap}${promo}</div>`;
    }

    // ================================================================
    // RESET
    // ================================================================
    document.getElementById('reset-btn').addEventListener('click',()=>{
      promoActive=false;
      document.getElementById('promo-overlay').classList.remove('active');
      fetch('/reset/'+ROOM,{method:'POST'});
    });

    // ================================================================
    // CLICK HANDLER
    // ================================================================
    img.addEventListener('click', async function(e) {
      if(promoActive) return;
      ensureAudio();  // ensure audio context created on user gesture
      const rect=img.getBoundingClientRect();
      const resp=await fetch('/click/'+ROOM,{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({x:e.clientX-rect.left, y:e.clientY-rect.top,
                             imgW:rect.width, imgH:rect.height})
      });
      const data=await resp.json();
      playSound(data.event);
    });
  </script>
</body>
</html>'''


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)

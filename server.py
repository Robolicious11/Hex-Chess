import os
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

import pygame
import pygame.gfxdraw
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

WHITE_PIECE_FG  = (248, 242, 226)
WHITE_PIECE_OL  = (42,  35,  24)
BLACK_PIECE_FG  = (28,  22,  16)
BLACK_PIECE_OL  = (205, 196, 178)

# Board color themes are a per-room setting (chosen at /new, like time_limit
# or difficulty) rather than a per-viewer preference like Flip, since the
# board is rendered server-side into one shared PNG for both players.
# Piece and highlight colors stay constant across themes — only tile/
# border/background colors vary — to avoid any contrast/legibility risk.
BOARD_THEMES = {
    "classic": {
        "base":   [(210, 200, 186), (180, 168, 152), (148, 134, 116)],
        "border": (90, 80, 68),
        "bg":     (232, 225, 214),
    },
    "ocean": {
        "base":   [(198, 214, 222), (162, 184, 196), (128, 154, 170)],
        "border": (54, 78, 94),
        "bg":     (214, 226, 232),
    },
    "forest": {
        "base":   [(202, 210, 182), (172, 186, 148), (140, 156, 112)],
        "border": (66, 80, 50),
        "bg":     (222, 228, 206),
    },
}
DEFAULT_BOARD_THEME = "classic"

ROOM_INACTIVITY_TIMEOUT = 2 * 60 * 60   # purge rooms idle longer than this
REAPER_SWEEP_INTERVAL   = 5 * 60        # how often the reaper checks

pygame.init()
surface     = pygame.Surface((WIDTH, HEIGHT))
label_font  = pygame.font.Font(FONT_PATH, 10)
_pfcache    = {}
_hex_overlay_cache = {}
_panel_glow_cache  = {}
render_lock = threading.Lock()
rooms       = {}
rooms_lock  = threading.Lock()


# ---------------------------------------------------------------------------
# Room helpers
# ---------------------------------------------------------------------------

def make_room(time_limit=300, ai=False, ai_difficulty="medium", increment=0,
              theme=DEFAULT_BOARD_THEME):
    now = time.time()
    tl  = float(time_limit) if time_limit > 0 else None
    return {
        "game":              Game(size=BOARD_SIZE),
        "selected":          None,
        "legal_moves":       [],
        "winner":            None,
        "win_reason":        None,
        "draw_offered_by":   None,
        "pending_promotion": None,
        "last_move":         None,
        "history":           [],
        "white_time":        tl,
        "black_time":        tl,
        "init_time":         tl,
        "increment":         float(increment) if increment else 0.0,
        "clock_since":       now if tl else None,
        "ai":                ai,
        "ai_color":          "black" if ai else None,
        "ai_difficulty":     ai_difficulty,
        "ai_thinking":       False,
        "theme":             theme if theme in BOARD_THEMES else DEFAULT_BOARD_THEME,
        "last_event":        None,
        "event_seq":         0,
        "lock":              threading.Lock(),
        "created":           now,
        "last_activity":     now,
        "undo_stack":        [],
    }


def snapshot_room_state(room):
    """Capture everything a move can mutate, so a later undo can fully
    restore the room to exactly how it was before that move."""
    return {
        "game":            copy.deepcopy(room["game"]),
        "white_time":      room["white_time"],
        "black_time":      room["black_time"],
        "clock_since":     room["clock_since"],
        "winner":          room["winner"],
        "win_reason":      room["win_reason"],
        "last_move":       room["last_move"],
        "history":         list(room["history"]),
        "draw_offered_by": room["draw_offered_by"],
    }


def restore_room_state(room, snap):
    room["game"]              = snap["game"]
    room["white_time"]        = snap["white_time"]
    room["black_time"]        = snap["black_time"]
    room["clock_since"]       = snap["clock_since"]
    room["winner"]            = snap["winner"]
    room["win_reason"]        = snap["win_reason"]
    room["last_move"]         = snap["last_move"]
    room["history"]           = snap["history"]
    room["draw_offered_by"]   = snap["draw_offered_by"]
    room["selected"]          = None
    room["legal_moves"]       = []
    room["pending_promotion"] = None


def get_room(room_id):
    with rooms_lock:
        return rooms.get(room_id)


NEW_ROOM_RATE_LIMIT  = 10    # max room creations...
NEW_ROOM_RATE_WINDOW = 600   # ...per this many seconds, per client IP

_new_room_hits      = {}
_new_room_hits_lock = threading.Lock()


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit_new_room():
    """Returns True if this client may create another room right now, and
    records the attempt. Guards against a rapid /new burst spiking memory
    faster than the room reaper's sweep interval can clean it up."""
    ip = _client_ip()
    now = time.time()
    with _new_room_hits_lock:
        hits = [t for t in _new_room_hits.get(ip, []) if now - t < NEW_ROOM_RATE_WINDOW]
        if len(hits) >= NEW_ROOM_RATE_LIMIT:
            _new_room_hits[ip] = hits
            return False
        hits.append(now)
        _new_room_hits[ip] = hits
        return True


def reap_inactive_rooms():
    """Background sweep that purges rooms nobody has touched in a while,
    so a publicly-reachable /new endpoint can't grow the process forever."""
    while True:
        time.sleep(REAPER_SWEEP_INTERVAL)
        now = time.time()
        with rooms_lock:
            snapshot = list(rooms.items())
        stale_ids = []
        for room_id, room in snapshot:
            with room["lock"]:
                if now - room["last_activity"] > ROOM_INACTIVITY_TIMEOUT:
                    stale_ids.append(room_id)
        if stale_ids:
            with rooms_lock:
                for room_id in stale_ids:
                    rooms.pop(room_id, None)

        with _new_room_hits_lock:
            for ip in list(_new_room_hits.keys()):
                hits = [t for t in _new_room_hits[ip] if now - t < NEW_ROOM_RATE_WINDOW]
                if hits:
                    _new_room_hits[ip] = hits
                else:
                    del _new_room_hits[ip]


threading.Thread(target=reap_inactive_rooms, daemon=True).start()


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
        room[key] = max(0.0, room[key] - elapsed) + (room.get("increment") or 0)
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

            room["undo_stack"].append(snapshot_room_state(room))
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


def _hex_points(x, y, size):
    return [(int(x + size * math.cos(math.radians(60 * i))),
             int(y + size * math.sin(math.radians(60 * i)))) for i in range(6)]


def draw_hex(x, y, size, fill, border):
    pts = _hex_points(x, y, size)
    # filled_polygon (hard-edged) + aapolygon (anti-aliased outline) is the
    # standard pygame idiom for a smoother-looking polygon than a plain
    # pygame.draw.polygon, without needing per-pixel supersampling.
    pygame.gfxdraw.filled_polygon(surface, pts, fill)
    pygame.gfxdraw.aapolygon(surface, pts, border)


def get_hex_overlay(size, color, alpha=130):
    """Cached translucent hex, blitted on top of a tile's normal color so
    highlights read as a tint over the board's texture rather than a flat
    opaque repaint."""
    key = (size, color, alpha)
    cached = _hex_overlay_cache.get(key)
    if cached is None:
        pad = size + 2
        cached = pygame.Surface((pad * 2, pad * 2), pygame.SRCALPHA)
        pts = _hex_points(pad, pad, size)
        pygame.gfxdraw.filled_polygon(cached, pts, (*color, alpha))
        _hex_overlay_cache[key] = cached
    return cached


def get_panel_glow(w, h, color):
    """Cached soft glow behind the game-over panel: pygame has no real blur,
    so a few nested rounded rects with increasing alpha toward the center
    approximate one cheaply."""
    key = (w, h, color)
    cached = _panel_glow_cache.get(key)
    if cached is None:
        pad = 36
        cached = pygame.Surface((w + pad * 2, h + pad * 2), pygame.SRCALPHA)
        for inset, alpha in ((pad, 16), (pad * 2 // 3, 28), (pad // 3, 42)):
            rect = (inset, inset, w + pad * 2 - inset * 2, h + pad * 2 - inset * 2)
            pygame.draw.rect(cached, (*color, alpha), rect, border_radius=24)
        _panel_glow_cache[key] = cached
    return cached


def draw_piece(sym, fg, ol, font, cx, cy):
    shadow = font.render(sym, True, (0, 0, 0))
    shadow.set_alpha(80)
    surface.blit(shadow, shadow.get_rect(center=(cx + 2, cy + 3)))

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


def render_room(room, flip=False):
    game      = room["game"]
    selected  = room["selected"]
    legal_set = set(room["legal_moves"])
    lm        = room.get("last_move") or {}
    last_from = lm.get("from")
    last_to   = lm.get("to")
    theme     = BOARD_THEMES.get(room.get("theme"), BOARD_THEMES[DEFAULT_BOARD_THEME])

    in_check_now = not room.get("winner") and game.is_in_check(game.turn)
    king_pos = None
    if in_check_now:
        for pos, piece in game.board.items():
            if piece and piece.name == "king" and piece.owner == game.turn:
                king_pos = pos
                break

    surface.fill(theme["bg"])

    for (q, r), piece in game.board.items():
        # Flip is a purely visual, per-viewer rotation: draw the true square
        # (q, r) at the pixel position of its 180-degree-rotated counterpart,
        # while every lookup below (highlight, label, piece) still keys off
        # the real board coordinate.
        pq, pr = (-q, -r) if flip else (q, r)
        x, y, ts = game.to_pixel(pq, pr, WIDTH, HEIGHT, zoom=ZOOM)
        hr = int(ts * DRAW_SCALE)

        draw_hex(x, y, hr, theme["base"][(q - r) % 3], theme["border"])

        if selected == (q, r):
            highlight = (245, 200, 28)
        elif (q, r) in legal_set:
            highlight = (88, 182, 106)
        elif (q, r) == king_pos:
            highlight = (196, 46, 46)
        elif (q, r) == last_to:
            highlight = (80, 138, 205)
        elif (q, r) == last_from:
            highlight = (138, 182, 225)
        else:
            highlight = None

        if highlight:
            overlay = get_hex_overlay(hr, highlight)
            surface.blit(overlay, overlay.get_rect(center=(x, y)))

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

        win_reason = room.get("win_reason")
        if winner == "draw" and win_reason == "draw":
            title_text, accent = "Draw — repetition / 50-move rule", (200, 200, 255)
        elif winner == "draw" and win_reason == "agreement":
            title_text, accent = "Draw by agreement", (200, 200, 255)
        elif winner == "draw":
            title_text, accent = "Stalemate — Draw!", (200, 200, 255)
        elif win_reason == "timeout":
            loser = "black" if winner == "white" else "white"
            title_text, accent = f"{loser.capitalize()} ran out of time!", (255, 218, 48)
        elif win_reason == "resignation":
            title_text, accent = f"{winner.capitalize()} wins by resignation!", (255, 218, 48)
        else:
            title_text, accent = f"{winner.capitalize()} wins by checkmate!", (255, 218, 48)
        glyph = "♔  ♚" if winner == "draw" else ("♔" if winner == "white" else "♚")

        panel_w, panel_h = 460, 190
        panel_x, panel_y = (WIDTH - panel_w) // 2, HEIGHT // 2 - panel_h // 2

        glow = get_panel_glow(panel_w, panel_h, accent)
        surface.blit(glow, glow.get_rect(center=(WIDTH // 2, HEIGHT // 2)))
        pygame.draw.rect(surface, (24, 27, 38), (panel_x, panel_y, panel_w, panel_h), border_radius=18)
        pygame.draw.rect(surface, accent, (panel_x, panel_y, panel_w, panel_h), 2, border_radius=18)

        glyph_font = pygame.font.Font(FONT_PATH, 60)
        big = pygame.font.Font(FONT_PATH, 25)
        sub = pygame.font.Font(FONT_PATH, 15)

        glyph_surf = glyph_font.render(glyph, True, accent)
        title = big.render(title_text, True, (240, 240, 245))
        hint  = sub.render("Press Reset Game to play again", True, (170, 170, 178))

        surface.blit(glyph_surf, glyph_surf.get_rect(center=(WIDTH // 2, panel_y + 52)))
        surface.blit(title, title.get_rect(center=(WIDTH // 2, panel_y + 122)))
        surface.blit(hint,  hint.get_rect(center=(WIDTH // 2, panel_y + 160)))
    else:
        in_check = game.is_in_check(game.turn)
        bg  = (168, 26, 26) if in_check else ((238, 235, 228) if game.turn == "white" else (26, 26, 26))
        fg  = (255, 255, 255) if (in_check or game.turn == "black") else (0, 0, 0)
        tw  = "White" if game.turn == "white" else "Black"
        lbl = f"{tw} is in CHECK!" if in_check else f"{tw}'s turn"
        lf  = pygame.font.Font(FONT_PATH, 16)
        bw  = 212 if in_check else 152
        pygame.draw.rect(surface, bg,             (10, 10, bw, 34), border_radius=8)
        pygame.draw.rect(surface, theme["border"], (10, 10, bw, 34), 2, border_radius=8)
        surface.blit(lf.render(lbl, True, fg),
                     lf.render(lbl, True, fg).get_rect(center=(10 + bw // 2, 27)))

        if room.get("ai_thinking"):
            tf  = pygame.font.Font(FONT_PATH, 13)
            txt = "AI is thinking…"
            tw2 = tf.size(txt)[0]
            bx, by, bwid, bhei = 10, 50, tw2 + 20, 26
            pygame.draw.rect(surface, (44, 44, 44),   (bx, by, bwid, bhei), border_radius=7)
            pygame.draw.rect(surface, theme["border"], (bx, by, bwid, bhei), 2, border_radius=7)
            surface.blit(tf.render(txt, True, (150, 190, 230)),
                         tf.render(txt, True, (150, 190, 230)).get_rect(center=(bx + bwid // 2, by + bhei // 2)))

        init = room.get("init_time")
        if init is not None:
            cx    = WIDTH - cw - 10
            b_rem = get_time_remaining(room, "black")
            w_rem = get_time_remaining(room, "white")
            inc   = room.get("increment") or 0
            inc_suffix = f" +{int(inc)}" if inc else ""
            draw_clock_badge(cx, 10,               cw, ch, "BLACK" + inc_suffix,
                             fmt_time(b_rem), game.turn == "black", b_rem, init, cfont, sfont)
            draw_clock_badge(cx, HEIGHT - ch - 10, cw, ch, "WHITE" + inc_suffix,
                             fmt_time(w_rem), game.turn == "white", w_rem, init, cfont, sfont)


def get_frame_bytes(room, flip=False):
    with render_lock:
        with room["lock"]:
            check_timer_expiry(room)
            render_room(room, flip=flip)
        buf = io.BytesIO()
        pygame.image.save(surface, buf, "frame.png")
        buf.seek(0)
        return buf.read()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    preview_url = url_for('preview_image', _external=True)
    return render_template_string(LANDING_HTML, preview_url=preview_url)


@app.route('/preview.png')
def preview_image():
    """Generic starting-position board image, used as the Open Graph /
    Twitter preview thumbnail when the game link is shared."""
    preview_room = make_room()
    return Response(get_frame_bytes(preview_room), mimetype='image/png',
                    headers={'Cache-Control': 'public, max-age=3600'})


@app.route('/new', methods=['POST'])
def new_game():
    if not rate_limit_new_room():
        return jsonify({'ok': False, 'error': 'Too many games created recently, please wait a bit.'}), 429

    room_id = uuid.uuid4().hex[:10]
    try:
        time_limit = int(request.form.get('time_limit', 300))
    except (ValueError, TypeError):
        time_limit = 300
    try:
        increment = int(request.form.get('increment', 0))
    except (ValueError, TypeError):
        increment = 0
    if increment not in (0, 2, 5, 10):
        increment = 0
    ai         = request.form.get('ai', '0') == '1'
    difficulty = request.form.get('difficulty', 'medium')
    if difficulty not in ('easy', 'medium', 'hard'):
        difficulty = 'medium'
    theme = request.form.get('theme', DEFAULT_BOARD_THEME)
    if theme not in BOARD_THEMES:
        theme = DEFAULT_BOARD_THEME
    with rooms_lock:
        rooms[room_id] = make_room(time_limit=time_limit, ai=ai, ai_difficulty=difficulty,
                                    increment=increment, theme=theme)
    return redirect(url_for('game_page', room_id=room_id))


def _render_game_page(room_id, spectate):
    room = get_room(room_id)
    if room is None:
        return "Game not found. <a href='/'>Create a new game</a>", 404
    first_frame = base64.b64encode(get_frame_bytes(room)).decode('utf-8')
    preview_url = url_for('preview_image', _external=True)
    return render_template_string(GAME_HTML, room_id=room_id,
                                  first_frame=first_frame,
                                  ai_mode=room.get("ai", False),
                                  ai_difficulty=room.get("ai_difficulty", "medium"),
                                  preview_url=preview_url,
                                  spectate=spectate)


@app.route('/game/<room_id>')
def game_page(room_id):
    return _render_game_page(room_id, spectate=False)


@app.route('/watch/<room_id>')
def watch_page(room_id):
    """Read-only spectator view: same trust model as the rest of this
    no-login app (the underlying routes are still reachable directly),
    this just hides the controls and never attaches the click handler."""
    return _render_game_page(room_id, spectate=True)


@app.route('/frame/<room_id>')
def frame(room_id):
    room = get_room(room_id)
    if room is None:
        return '', 404
    flip = request.args.get('flip') == '1'
    return Response(get_frame_bytes(room, flip=flip), mimetype='image/png',
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
        lm = room.get("last_move")
        last_move_json = None
        if lm:
            moved_piece = room["game"].board.get(lm["to"])
            last_move_json = {
                "from": list(lm["from"]),
                "to":   list(lm["to"]),
                "sym":  PIECE_SYMBOLS[moved_piece.owner][moved_piece.name] if moved_piece else None,
            }
        return jsonify({
            'pending_promotion': room["pending_promotion"] is not None,
            'turn':              room["game"].turn,
            'event_seq':         room["event_seq"],
            'last_event':        room["last_event"],
            'winner':            room["winner"] is not None,
            'draw_offered_by':   room["draw_offered_by"],
            'last_move':         last_move_json,
            'ai_color':          room.get("ai_color"),
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
    flip  = bool(data.get('flip', False))

    ai_triggered = False
    click_event  = None

    with room["lock"]:
        room["last_activity"] = time.time()
        check_timer_expiry(room)
        if room["winner"] or room["pending_promotion"]:
            return jsonify({'ok': True})

        game = room["game"]
        if room["ai"] and game.turn == room["ai_color"]:
            return jsonify({'ok': True})

        q, r = game.from_pixel(mx, my, WIDTH, HEIGHT, zoom=ZOOM)
        if flip:
            q, r = -q, -r
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
                room["undo_stack"].append(snapshot_room_state(room))
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


def _reset_game_fields(room):
    """Reset the game itself back to a fresh starting position, leaving
    room-level settings (time control, increment, theme, ai/difficulty)
    untouched. Caller must already hold room["lock"]."""
    init = room["init_time"]
    room["game"]              = Game(size=BOARD_SIZE)
    room["selected"]          = None
    room["legal_moves"]       = []
    room["winner"]            = None
    room["win_reason"]        = None
    room["draw_offered_by"]   = None
    room["pending_promotion"] = None
    room["last_move"]         = None
    room["history"]           = []
    room["white_time"]        = init
    room["black_time"]        = init
    room["clock_since"]       = time.time() if init else None
    room["last_event"]        = None
    room["ai_thinking"]       = False
    room["undo_stack"]        = []


@app.route('/reset/<room_id>', methods=['POST'])
def reset(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    with room["lock"]:
        room["last_activity"] = time.time()
        _reset_game_fields(room)
        room["event_seq"]    += 1
    return jsonify({'ok': True})


@app.route('/rematch/<room_id>', methods=['POST'])
def rematch(room_id):
    """Reset a vs-AI room and swap which color the AI plays. 2-player rooms
    have no color binding to swap (no player identity exists anywhere in
    this app), so this route is AI-only; Reset Game covers the 2P case."""
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    if not room["ai"]:
        return jsonify({'ok': False, 'error': 'rematch with side-swap only applies to vs-AI games'}), 400

    ai_triggered = False
    with room["lock"]:
        room["last_activity"] = time.time()
        _reset_game_fields(room)
        room["ai_color"]  = "white" if room["ai_color"] == "black" else "black"
        room["event_seq"] += 1
        if room["game"].turn == room["ai_color"]:
            ai_triggered = True

    if ai_triggered:
        threading.Thread(target=trigger_ai_move, args=(room_id,), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/undo/<room_id>', methods=['POST'])
def undo(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404

    with room["lock"]:
        room["last_activity"] = time.time()
        if room["pending_promotion"]:
            return jsonify({'ok': False, 'error': 'cannot undo during pending promotion'}), 400
        if room["ai_thinking"]:
            return jsonify({'ok': False, 'error': 'AI is thinking'}), 400
        if not room["undo_stack"]:
            return jsonify({'ok': False, 'error': 'nothing to undo'}), 400

        # In vs-AI rooms, undo reverts both the AI's reply and the human's
        # move that provoked it, so one click gives back "my move" as a
        # human would expect. In 2P rooms it reverts just the last move,
        # consistent with how resign/draw already treat either side's tab
        # as equally trusted to act (there is no player identity here).
        ply_count = 2 if room["ai"] else 1
        ply_count = min(ply_count, len(room["undo_stack"]))
        snap = None
        for _ in range(ply_count):
            snap = room["undo_stack"].pop()
        restore_room_state(room, snap)
        room["last_event"] = "move"
        room["event_seq"] += 1
    return jsonify({'ok': True})


@app.route('/resign/<room_id>', methods=['POST'])
def resign(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    color = (request.json or {}).get('color')
    if color not in ('white', 'black'):
        return jsonify({'ok': False, 'error': 'invalid color'}), 400

    with room["lock"]:
        room["last_activity"] = time.time()
        if room["winner"] or room["pending_promotion"]:
            return jsonify({'ok': False, 'error': 'game already over'}), 400
        if room["ai"] and color == room["ai_color"]:
            return jsonify({'ok': False, 'error': 'cannot resign on behalf of the AI'}), 400
        room["winner"]      = "black" if color == "white" else "white"
        room["win_reason"]  = "resignation"
        room["clock_since"] = None
        room["last_event"]  = "checkmate"
        room["event_seq"]  += 1
    return jsonify({'ok': True})


@app.route('/draw_offer/<room_id>', methods=['POST'])
def draw_offer(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    color = (request.json or {}).get('color')
    if color not in ('white', 'black'):
        return jsonify({'ok': False, 'error': 'invalid color'}), 400

    with room["lock"]:
        room["last_activity"] = time.time()
        if room["ai"]:
            return jsonify({'ok': False, 'error': 'AI opponent cannot respond to draw offers'}), 400
        if room["winner"] or room["pending_promotion"]:
            return jsonify({'ok': False, 'error': 'game already over'}), 400
        if room["draw_offered_by"]:
            return jsonify({'ok': False, 'error': 'a draw offer is already pending'}), 400
        room["draw_offered_by"] = color
        room["event_seq"]      += 1
    return jsonify({'ok': True})


@app.route('/draw_respond/<room_id>', methods=['POST'])
def draw_respond(room_id):
    room = get_room(room_id)
    if room is None:
        return jsonify({'ok': False}), 404
    accept = bool((request.json or {}).get('accept'))

    with room["lock"]:
        room["last_activity"] = time.time()
        if not room["draw_offered_by"]:
            return jsonify({'ok': False, 'error': 'no draw offer pending'}), 400
        room["draw_offered_by"] = None
        if accept:
            room["winner"]      = "draw"
            room["win_reason"]  = "agreement"
            room["clock_since"] = None
            room["last_event"]  = "checkmate"
        room["event_seq"] += 1
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
        room["last_activity"] = time.time()
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
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hex Chess</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%231a2338'/%3E%3Ctext x='50' y='72' font-size='64' text-anchor='middle' fill='%23e8dfc8'%3E%E2%99%9E%3C/text%3E%3C/svg%3E">
  <meta property="og:title" content="Hex Chess">
  <meta property="og:description" content="Chess adapted to a hexagonal board — play a friend or an AI opponent, right in your browser.">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{{ request.url_root }}">
  <meta property="og:image" content="{{ preview_url }}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="Hex Chess">
  <meta name="twitter:description" content="Chess adapted to a hexagonal board — play a friend or an AI opponent, right in your browser.">
  <meta name="twitter:image" content="{{ preview_url }}">
  <style>
    *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
    :root {
      --bg-1:#1e2540; --bg-2:#0f1220;
      --text:#eee; --text-dim:#7a8090; --text-faint:#6a7080;
      --surface:rgba(255,255,255,0.04); --surface-border:rgba(255,255,255,0.1);
      --surface-hover-border:rgba(255,255,255,0.3); --surface-hover-text:#ddd;
      --field-bg:rgba(255,255,255,0.06); --field-border:rgba(255,255,255,0.14);
      --field-option-bg:#1a2030;
      --mode-border:rgba(255,255,255,0.12); --mode-bg:rgba(255,255,255,0.04);
      --mode-color:#aaa; --mode-desc:#666;
      --accent:#3a70b8; --accent-bright:#4a90d9;
      --accent-soft-bg:rgba(74,144,217,0.18); --accent-soft-text:#89b8e8;
      --ai-note-bg:rgba(74,144,217,0.1); --ai-note-text:#6a9fc8;
      --title-grad-1:#e8dfc8; --title-grad-2:#a89060;
      --btn-grad-1:#3a80c8; --btn-grad-2:#2060a8;
      --btn-hover-grad-1:#4a90d8; --btn-hover-grad-2:#3070b8;
      --hex-glow-1:rgba(232,223,200,0.035); --hex-glow-2:rgba(74,144,217,0.05);
      --preview-border:rgba(255,255,255,0.08);
      --icon-btn-bg:rgba(255,255,255,0.08); --icon-btn-border:rgba(255,255,255,0.16);

      /* Spacing / radius / font-size scale — not theme-dependent, so
         defined once here rather than repeated in the light override. */
      --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px; --space-5:24px;
      --radius-xs:6px; --radius-sm:8px; --radius-md:10px; --radius-lg:12px;
      --radius-xl:16px; --radius-2xl:20px; --radius-pill:999px;
      --fs-btn:0.88rem; --fs-btn-sm:0.78rem; --fs-card-title:1.2rem;
    }
    :root[data-theme="light"] {
      --bg-1:#eef1f7; --bg-2:#dde3ee;
      --text:#1c2430; --text-dim:#5c6675; --text-faint:#6b7686;
      --surface:rgba(20,30,50,0.035); --surface-border:rgba(20,30,50,0.12);
      --surface-hover-border:rgba(20,30,50,0.28); --surface-hover-text:#222;
      --field-bg:rgba(20,30,50,0.045); --field-border:rgba(20,30,50,0.16);
      --field-option-bg:#eef1f7;
      --mode-border:rgba(20,30,50,0.14); --mode-bg:rgba(20,30,50,0.03);
      --mode-color:#556; --mode-desc:#7a8494;
      --accent:#2f5f9e; --accent-bright:#3a7bc8;
      --accent-soft-bg:rgba(58,123,200,0.16); --accent-soft-text:#1f4a80;
      --ai-note-bg:rgba(58,123,200,0.1); --ai-note-text:#2f5f9e;
      --title-grad-1:#6b5220; --title-grad-2:#3d2e10;
      --btn-grad-1:#3a80c8; --btn-grad-2:#2060a8;
      --btn-hover-grad-1:#4a90d8; --btn-hover-grad-2:#3070b8;
      --hex-glow-1:rgba(60,50,20,0.03); --hex-glow-2:rgba(58,123,200,0.06);
      --preview-border:rgba(20,30,50,0.1);
      --icon-btn-bg:rgba(20,30,50,0.05); --icon-btn-border:rgba(20,30,50,0.18);
    }
    body {
      background: radial-gradient(ellipse at 50% 30%, var(--bg-1) 0%, var(--bg-2) 100%);
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      min-height:100vh; font-family:'Segoe UI', system-ui, sans-serif; color:var(--text);
      position:relative; overflow-x:hidden;
    }
    /* Two large, very faint hex silhouettes peeking from opposite corners —
       a subtle thematic nod to "hex chess", not meant to be noticed. */
    body::before, body::after {
      content:''; position:fixed; z-index:0; width:900px; height:900px;
      clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%);
      pointer-events:none;
    }
    body::before { top:-260px; left:-320px; background:var(--hex-glow-1); }
    body::after  { bottom:-300px; right:-280px; background:var(--hex-glow-2); }

    #theme-toggle { position:fixed; top:16px; right:16px; z-index:2;
      padding:var(--space-2) var(--space-3); background:var(--icon-btn-bg); color:var(--text);
      border:1px solid var(--icon-btn-border); border-radius:var(--radius-xs); cursor:pointer;
      font-size:0.9rem; transition:background 0.18s, border-color 0.18s; }
    #theme-toggle:hover { background:var(--surface-hover-border); }

    #landing-wrap {
      display:flex; align-items:center; justify-content:center; gap:32px;
      max-width:920px; width:100%; padding:20px; position:relative; z-index:1;
    }
    #landing-preview { flex:0 0 320px; display:flex; justify-content:center; }
    #landing-preview img {
      width:100%; max-width:320px; border-radius:var(--radius-xl);
      border:1px solid var(--preview-border); box-shadow:0 20px 50px rgba(0,0,0,0.5);
    }
    @media (max-width: 820px) {
      #landing-wrap { flex-direction:column; }
      #landing-preview { flex:none; width:min(320px, 80vw); }
    }

    .card {
      background:var(--surface); border:1px solid var(--surface-border);
      border-radius:var(--radius-2xl); padding:40px 48px 36px; width:min(440px, 92vw);
      display:flex; flex-direction:column; align-items:center;
      box-shadow:0 24px 60px rgba(0,0,0,0.5);
    }
    h1 {
      font-size:2.4rem; letter-spacing:6px;
      background:linear-gradient(135deg, var(--title-grad-1) 30%, var(--title-grad-2) 100%);
      -webkit-background-clip:text; -webkit-text-fill-color:transparent;
      margin-bottom:6px;
    }
    .subtitle { color:var(--text-dim); font-size:0.88rem; margin-bottom:28px; letter-spacing:0.5px; }

    .mode-row { display:flex; gap:12px; width:100%; margin-bottom:20px; }
    .mode-btn {
      flex:1; padding:16px 10px; border:2px solid var(--mode-border);
      background:var(--mode-bg); border-radius:var(--radius-lg); color:var(--mode-color);
      cursor:pointer; transition:all 0.18s; text-align:center; user-select:none;
    }
    .mode-btn .icon { font-size:1.8rem; display:block; margin-bottom:6px; }
    .mode-btn .name { font-size:0.82rem; font-weight:700; letter-spacing:1.5px; display:block; }
    .mode-btn .desc { font-size:0.72rem; color:var(--mode-desc); margin-top:4px; display:block; }
    .mode-btn:hover  { border-color:var(--surface-hover-border); color:var(--surface-hover-text); }
    .mode-btn.active { border-color:var(--accent-bright); background:var(--accent-soft-bg); color:var(--text); }
    .mode-btn.active .desc { color:var(--accent-soft-text); }

    .section-divider {
      width:100%; display:flex; align-items:center; gap:10px;
      margin:6px 0 16px; color:var(--text-faint); font-size:0.68rem;
      letter-spacing:2px; text-transform:uppercase;
    }
    .section-divider::before, .section-divider::after {
      content:''; flex:1; height:1px; background:var(--surface-border);
    }

    .field { width:100%; margin-bottom:18px; }
    .field label { display:block; font-size:0.75rem; letter-spacing:1.5px;
                   color:var(--text-faint); margin-bottom:8px; }
    .field select {
      width:100%; padding:11px 14px; background:var(--field-bg);
      color:var(--text); border:1px solid var(--field-border); border-radius:var(--radius-sm);
      font-size:0.95rem; cursor:pointer; outline:none; appearance:none;
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%23888' d='M6 8L0 0h12z'/%3E%3C/svg%3E");
      background-repeat:no-repeat; background-position:right 14px center;
    }
    .field select option { background:var(--field-option-bg); color:var(--text); }

    #ai-fields { width:100%; }
    .ai-note {
      font-size:0.78rem; color:var(--ai-note-text); margin-bottom:18px; width:100%;
      padding:8px 12px; background:var(--ai-note-bg);
      border-radius:var(--radius-xs); border-left:3px solid var(--accent-bright);
    }

    .create-btn {
      width:100%; padding:14px; font-size:1rem; font-weight:700; letter-spacing:2px;
      background:linear-gradient(135deg,var(--btn-grad-1),var(--btn-grad-2)); color:#fff; border:none;
      border-radius:var(--radius-md); cursor:pointer; transition:all 0.18s;
      box-shadow:0 4px 18px rgba(42,100,180,0.45);
    }
    .create-btn:hover {
      background:linear-gradient(135deg,var(--btn-hover-grad-1),var(--btn-hover-grad-2));
      box-shadow:0 6px 24px rgba(42,100,180,0.6); transform:translateY(-1px);
    }
  </style>
  <script>
    (function() {
      var saved = localStorage.getItem('hexchess_theme');
      if (saved === 'light') document.documentElement.dataset.theme = 'light';
    })();
  </script>
</head>
<body>
  <button id="theme-toggle" title="Toggle light/dark theme">🌙</button>
  <div id="landing-wrap">
    <div id="landing-preview">
      <img src="{{ preview_url }}" alt="A hex chess board">
    </div>
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

        <div class="section-divider">Match Settings</div>

        <div class="field">
          <label for="theme">BOARD THEME</label>
          <select name="theme" id="theme">
            <option value="classic" selected>Classic — tan &amp; brown</option>
            <option value="ocean">Ocean — blue &amp; gray</option>
            <option value="forest">Forest — green</option>
          </select>
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

        <div class="field">
          <label for="increment">INCREMENT (BONUS PER MOVE)</label>
          <select name="increment" id="increment">
            <option value="0" selected>None</option>
            <option value="2">+2 seconds</option>
            <option value="5">+5 seconds</option>
            <option value="10">+10 seconds</option>
          </select>
        </div>

        <button type="submit" class="create-btn">CREATE GAME</button>
      </div>
    </form>
  </div>
  <script>
    function setMode(ai) {
      document.getElementById('ai_hidden').value = ai ? '1' : '0';
      document.getElementById('btn-multi').classList.toggle('active', !ai);
      document.getElementById('btn-ai').classList.toggle('active',  !!ai);
      document.getElementById('ai-fields').style.display = ai ? '' : 'none';
    }

    const themeToggle = document.getElementById('theme-toggle');
    function updateThemeToggle() {
      themeToggle.textContent = document.documentElement.dataset.theme === 'light' ? '☀️' : '🌙';
    }
    updateThemeToggle();
    themeToggle.addEventListener('click', () => {
      const isLight = document.documentElement.dataset.theme === 'light';
      if (isLight) delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = 'light';
      localStorage.setItem('hexchess_theme', isLight ? 'dark' : 'light');
      updateThemeToggle();
    });
  </script>
</body>
</html>'''


GAME_HTML = r'''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hex Chess</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%231a2338'/%3E%3Ctext x='50' y='72' font-size='64' text-anchor='middle' fill='%23e8dfc8'%3E%E2%99%9E%3C/text%3E%3C/svg%3E">
  <meta property="og:title" content="Join my Hex Chess game">
  <meta property="og:description" content="Chess adapted to a hexagonal board — click the link to play.">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{{ request.url }}">
  <meta property="og:image" content="{{ preview_url }}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="Join my Hex Chess game">
  <meta name="twitter:description" content="Chess adapted to a hexagonal board — click the link to play.">
  <meta name="twitter:image" content="{{ preview_url }}">
  <style>
    *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
    :root {
      --bg-1:#1e2540; --bg-2:#0f1220;
      --text:#eee; --text-dim:#90a0b0; --text-faint:#5a6878; --text-faint2:#3a4858;
      --heading:#e0d8c8;
      --surface:rgba(255,255,255,0.04); --surface-border:rgba(255,255,255,0.09);
      --surface-border-soft:rgba(255,255,255,0.07);
      --history-alt:rgba(255,255,255,0.02);
      --scrollbar:#334;
      --icon-btn-bg:rgba(255,255,255,0.08); --icon-btn-border:rgba(255,255,255,0.16);
      --icon-btn-hover-bg:rgba(255,255,255,0.18);
      --ghost-bg:rgba(255,255,255,0.06); --ghost-border:rgba(255,255,255,0.18);
      --ghost-hover-bg:rgba(255,255,255,0.14); --ghost-text:#dde3ea;
      --accent:#3a70b8; --accent-bright:#4a90d9; --accent-hover:#4a84d0;
      --accent-soft-bg:rgba(74,144,217,0.35);
      --share-url-bg:rgba(255,255,255,0.06); --share-url-text:#8ac0e0;
      --share-url-border:rgba(74,144,217,0.4);
      --ai-badge-text:#6aacda; --ai-badge-bg:rgba(106,172,218,0.1); --ai-badge-border:rgba(106,172,218,0.3);
      --board-shadow-ring:rgba(255,255,255,0.07);
      --white-entry:#e0d8c8; --black-entry:#9098a8;
      --capture-dot:#c05050;
      --promo-badge-text:#d4a020; --promo-badge-bg:rgba(212,160,32,0.15);
      --capture-diff:#5aa86a;
      --resign-text:#e8a0a0; --resign-border:rgba(200,90,90,0.5);
      --resign-bg:rgba(200,60,60,0.08); --resign-hover-bg:rgba(200,60,60,0.22); --resign-hover-text:#ffb8b8;
      --dialog-bg:#1a2338; --dialog-text-dim:#7a90a8;
      --choice-cancel-text:#8a96a8; --choice-cancel-border:#445; --choice-cancel-hover:#cdd;
      --promo-label:#89aacf;
      --draw-banner-bg:rgba(58,112,184,0.18); --draw-banner-border:rgba(74,144,217,0.5); --draw-banner-text:#dce8f5;

      /* Spacing / radius / font-size scale — not theme-dependent, so
         defined once here rather than repeated in the light override. */
      --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px; --space-5:24px;
      --radius-xs:6px; --radius-sm:8px; --radius-md:10px; --radius-lg:12px;
      --radius-xl:16px; --radius-2xl:20px; --radius-pill:999px;
      --fs-btn:0.88rem; --fs-btn-sm:0.78rem; --fs-card-title:1.2rem;
    }
    :root[data-theme="light"] {
      --bg-1:#eef1f7; --bg-2:#dde3ee;
      --text:#1c2430; --text-dim:#54606e; --text-faint:#6b7686; --text-faint2:#8892a0;
      --heading:#3a3020;
      --surface:rgba(20,30,50,0.035); --surface-border:rgba(20,30,50,0.12);
      --surface-border-soft:rgba(20,30,50,0.09);
      --history-alt:rgba(20,30,50,0.025);
      --scrollbar:#b7c0cc;
      --icon-btn-bg:rgba(20,30,50,0.05); --icon-btn-border:rgba(20,30,50,0.18);
      --icon-btn-hover-bg:rgba(20,30,50,0.1);
      --ghost-bg:rgba(20,30,50,0.05); --ghost-border:rgba(20,30,50,0.2);
      --ghost-hover-bg:rgba(20,30,50,0.1); --ghost-text:#20303f;
      --accent:#2f5f9e; --accent-bright:#3a7bc8; --accent-hover:#3a70b8;
      --accent-soft-bg:rgba(58,123,200,0.28);
      --share-url-bg:rgba(20,30,50,0.05); --share-url-text:#2f5f9e;
      --share-url-border:rgba(58,123,200,0.4);
      --ai-badge-text:#2f5f9e; --ai-badge-bg:rgba(58,123,200,0.1); --ai-badge-border:rgba(58,123,200,0.3);
      --board-shadow-ring:rgba(20,30,50,0.1);
      --white-entry:#6b5a30; --black-entry:#3a4452;
      --capture-dot:#a83a3a;
      --promo-badge-text:#8a640f; --promo-badge-bg:rgba(180,130,20,0.15);
      --capture-diff:#2f7a4a;
      --resign-text:#a83a3a; --resign-border:rgba(168,50,50,0.4);
      --resign-bg:rgba(168,50,50,0.06); --resign-hover-bg:rgba(168,50,50,0.14); --resign-hover-text:#8a2020;
      --dialog-bg:#ffffff; --dialog-text-dim:#5a6f82;
      --choice-cancel-text:#6a7686; --choice-cancel-border:#c2c8d0; --choice-cancel-hover:#2c3644;
      --promo-label:#3f6a8a;
      --draw-banner-bg:rgba(58,123,200,0.12); --draw-banner-border:rgba(58,123,200,0.4); --draw-banner-text:#1c3550;
    }
    body {
      background:radial-gradient(ellipse at 50% 30%, var(--bg-1) 0%, var(--bg-2) 100%);
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      min-height:100vh; padding:16px; font-family:'Segoe UI', system-ui, sans-serif;
      color:var(--text);
    }
    h1 { color:var(--heading); font-size:1.25rem; letter-spacing:4px; margin-bottom:8px; }

    #theme-toggle { position:fixed; top:16px; right:16px; z-index:50;
      padding:var(--space-2) var(--space-3); background:var(--icon-btn-bg); color:var(--text);
      border:1px solid var(--icon-btn-border); border-radius:var(--radius-xs); cursor:pointer;
      font-size:0.9rem; transition:background 0.18s, border-color 0.18s; }
    #theme-toggle:hover { background:var(--icon-btn-hover-bg); }

    #share-box { display:flex; align-items:center; gap:8px; margin-bottom:10px; flex-wrap:wrap;
                 justify-content:center; max-width:100%; }
    #share-url { background:var(--share-url-bg); color:var(--share-url-text);
                 border:1px solid var(--share-url-border); border-radius:var(--radius-pill);
                 padding:var(--space-2) var(--space-3); font-size:var(--fs-btn-sm); font-family:monospace;
                 width:300px; max-width:60vw; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
    #share-url::before { content:'🔗 '; }
    #copy-btn { padding:var(--space-2) var(--space-4); background:var(--accent); color:#fff; border:none;
                border-radius:var(--radius-pill); cursor:pointer; font-size:var(--fs-btn-sm); transition:background 0.18s; }
    #copy-btn:hover { background:var(--accent-hover); }
    .icon-btn { padding:var(--space-2) var(--space-3); background:var(--icon-btn-bg); color:var(--ghost-text);
                border:1px solid var(--icon-btn-border); border-radius:var(--radius-xs); cursor:pointer;
                font-size:var(--fs-btn-sm); transition:background 0.18s, border-color 0.18s, transform 0.12s; }
    .icon-btn:hover  { background:var(--icon-btn-hover-bg); }
    .icon-btn.active { background:var(--accent-soft-bg); border-color:var(--accent-bright); color:var(--text); }
    #ai-badge { color:var(--ai-badge-text); font-size:var(--fs-btn-sm); letter-spacing:1px; margin-bottom:10px;
                background:var(--ai-badge-bg); border:1px solid var(--ai-badge-border);
                border-radius:var(--radius-pill); padding:var(--space-2) var(--space-4); display:inline-block; }

    #game-area { display:flex; align-items:flex-start; gap:14px; max-width:100%; }
    #game-wrap { position:relative; display:inline-block; line-height:0; }
    #game { cursor:pointer; border-radius:var(--radius-md); display:block;
            box-shadow:0 8px 40px rgba(0,0,0,0.6), 0 0 0 1px var(--board-shadow-ring);
            max-width:100%; max-height:74vh; }
    #overlay, #move-anim { position:absolute; top:0; left:0; pointer-events:none; border-radius:var(--radius-md); }

    #history-panel {
      width:200px; min-width:200px; background:var(--surface);
      border:1px solid var(--surface-border); border-radius:var(--radius-md);
      display:flex; flex-direction:column; max-height:74vh; overflow:hidden;
    }

    @media (max-width: 720px) {
      #game-area { flex-direction:column; align-items:center; }
      #history-panel { width:100%; min-width:0; max-height:28vh; }
    }
    #history-title { padding:10px 14px 8px; font-size:0.72rem; letter-spacing:2px;
                     color:var(--text-faint); border-bottom:1px solid var(--surface-border-soft); flex-shrink:0; }
    #history-list { overflow-y:auto; flex:1; padding:6px 0;
                    scrollbar-width:thin; scrollbar-color:var(--scrollbar) transparent; }
    #history-list::-webkit-scrollbar { width:4px; }
    #history-list::-webkit-scrollbar-thumb { background:var(--scrollbar); border-radius:2px; }
    @keyframes moveRowIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:translateY(0); } }
    .move-pair { padding:3px 14px; animation:moveRowIn 0.25s ease; }
    .move-pair:nth-child(even) { background:var(--history-alt); }
    .pair-num { font-size:0.68rem; color:var(--text-faint2); margin-bottom:1px; }
    .move-entry { display:flex; align-items:center; gap:5px; padding:2px 0; font-size:0.8rem; }
    .move-entry .sym { font-size:1rem; line-height:1; }
    .move-entry .coords { color:var(--text-dim); letter-spacing:0.3px; }
    .move-entry.white-entry .sym { color:var(--white-entry); }
    .move-entry.black-entry .sym { color:var(--black-entry); }
    .capture-dot { width:5px; height:5px; border-radius:50%; background:var(--capture-dot); flex-shrink:0; }
    .promo-badge { font-size:0.6rem; color:var(--promo-badge-text); background:var(--promo-badge-bg);
                   border-radius:3px; padding:1px 4px; }
    .history-empty { color:var(--text-faint2); font-size:0.78rem; text-align:center; padding:20px 14px; }

    #capture-tray { display:flex; gap:24px; justify-content:center; flex-wrap:wrap;
                    margin-top:10px; max-width:100%; }
    .capture-side { display:flex; align-items:center; gap:8px; }
    .capture-label { font-size:0.62rem; letter-spacing:1.5px; color:var(--text-faint); }
    .capture-pieces { font-size:1.05rem; letter-spacing:1px; color:var(--text-dim); min-height:1.2em; }
    .capture-diff { font-size:0.75rem; font-weight:700; color:var(--capture-diff); }

    #bottom-row { display:flex; align-items:center; justify-content:center; gap:12px;
                  margin-top:10px; flex-wrap:wrap; }
    #bottom-row button { padding:var(--space-2) var(--space-5); border-radius:var(--radius-sm); font-size:var(--fs-btn);
                 cursor:pointer; letter-spacing:0.5px; font-weight:600;
                 transition:background 0.18s, border-color 0.18s, opacity 0.18s, transform 0.12s; }
    #bottom-row button:disabled { opacity:0.4; cursor:default; }

    /* Offer Draw: the one "positive/social" committal action — solid fill
       using the app's existing accent blue (see promo-box/choice-btn). */
    #draw-btn { background:var(--accent); color:#fff; border:none; }
    #draw-btn:hover { background:var(--accent-hover); }

    /* Undo / Reset / Rematch: everyday utility actions — neutral outlined/ghost style. */
    #undo-btn, #reset-btn, #rematch-btn { background:var(--ghost-bg); color:var(--ghost-text);
                 border:1px solid var(--ghost-border); }
    #undo-btn:hover, #reset-btn:hover, #rematch-btn:hover { background:var(--ghost-hover-bg); }

    /* Resign: rare, consequential — muted caution outline, not a solid block. */
    #resign-btn { background:var(--resign-bg); color:var(--resign-text);
                 border:1px solid var(--resign-border); }
    #resign-btn:hover { background:var(--resign-hover-bg); color:var(--resign-hover-text); }

    #promo-overlay, #choice-overlay { display:flex; position:fixed; inset:0;
                     background:rgba(0,0,0,0.78); z-index:100;
                     align-items:center; justify-content:center;
                     opacity:0; visibility:hidden; pointer-events:none;
                     transition: opacity 0.2s ease, visibility 0s linear 0.2s; }
    #promo-overlay.active, #choice-overlay.active {
                     opacity:1; visibility:visible; pointer-events:auto;
                     transition: opacity 0.2s ease, visibility 0s linear 0s; }
    #promo-box, #choice-box { background:var(--dialog-bg); border:2px solid var(--accent); border-radius:var(--radius-xl);
                 padding:30px 38px; text-align:center; color:var(--text);
                 box-shadow:0 20px 60px rgba(0,0,0,0.6); max-width:92vw;
                 transform:scale(0.94); transition:transform 0.2s ease; }
    #promo-overlay.active #promo-box, #choice-overlay.active #choice-box { transform:scale(1); }
    #promo-box h2, #choice-box h2 { font-size:var(--fs-card-title); letter-spacing:3px; margin-bottom:6px; }
    #promo-box p, #choice-box p  { color:var(--dialog-text-dim); margin-bottom:22px; font-size:0.88rem; }
    .promo-btn { padding:12px 20px; margin:0 6px; font-size:1.7rem;
                 background:var(--accent-soft-bg); color:#fff; border:2px solid var(--accent);
                 border-radius:var(--radius-md); cursor:pointer; transition:all 0.15s; line-height:1; }
    .promo-btn:hover { background:var(--accent-bright); transform:scale(1.1); }
    .promo-label { display:block; font-size:0.65rem; margin-top:4px;
                   letter-spacing:1px; color:var(--promo-label); }

    .choice-btn { padding:12px 22px; margin:6px; font-size:0.95rem; font-weight:600;
                 background:var(--accent-soft-bg); color:#fff; border:2px solid var(--accent);
                 border-radius:var(--radius-md); cursor:pointer; transition:all 0.15s; }
    .choice-btn:hover { background:var(--accent-bright); }
    .choice-cancel { padding:10px 18px; margin:10px 6px 0; font-size:0.82rem;
                 background:transparent; color:var(--choice-cancel-text); border:1px solid var(--choice-cancel-border); border-radius:var(--radius-sm);
                 cursor:pointer; }
    .choice-cancel:hover { color:var(--choice-cancel-hover); }

    #draw-banner { display:flex; align-items:center; gap:10px; justify-content:center;
                   background:var(--draw-banner-bg); border:1px solid var(--draw-banner-border);
                   border-radius:var(--radius-sm); color:var(--draw-banner-text); font-size:0.85rem; flex-wrap:wrap;
                   opacity:0; visibility:hidden; pointer-events:none;
                   max-height:0; padding:0 16px; margin-bottom:0; overflow:hidden;
                   transition: opacity 0.2s ease, max-height 0.25s ease,
                               padding 0.25s ease, margin-bottom 0.25s ease,
                               visibility 0s linear 0.25s; }
    #draw-banner.active { opacity:1; visibility:visible; pointer-events:auto;
                   max-height:60px; padding:8px 16px; margin-bottom:10px;
                   transition: opacity 0.2s ease, max-height 0.25s ease,
                               padding 0.25s ease, margin-bottom 0.25s ease,
                               visibility 0s linear 0s; }
    .draw-accept, .draw-decline { padding:5px 14px; border:none; border-radius:var(--radius-xs);
                   font-size:0.78rem; cursor:pointer; color:#fff; }
    .draw-accept, .draw-decline { transition:background 0.18s, transform 0.12s; }
    .draw-accept { background:#2a8a4a; }
    .draw-accept:hover { background:#34a458; }
    .draw-decline { background:#8a2a2a; }
    .draw-decline:hover { background:#a43434; }

    /* Small tactile press feedback across the main interactive buttons
       (.choice-btn/.promo-btn already transition "all", which covers this). */
    #bottom-row button:active, .icon-btn:active, .choice-btn:active,
    .promo-btn:active, .draw-accept:active, .draw-decline:active { transform:scale(0.96); }
  </style>
  <script>
    (function() {
      var saved = localStorage.getItem('hexchess_theme');
      if (saved === 'light') document.documentElement.dataset.theme = 'light';
    })();
  </script>
</head>
<body>
  <button id="theme-toggle" title="Toggle light/dark theme">🌙</button>
  <h1>HEX CHESS</h1>
  {% if ai_mode %}
  <div id="ai-badge">⚔ VS AI — <span id="ai-side-text">you play White</span>
    {% if ai_difficulty == 'easy' %}· Easy{% elif ai_difficulty == 'hard' %}· Hard{% else %}· Medium{% endif %}
  </div>
  {% endif %}
  <div id="share-box">
    <span id="share-url"></span>
    <button id="copy-btn">Copy Link</button>
    {% if not spectate %}
    <button id="copy-watch-btn" class="icon-btn" title="Copy a watch-only link for spectators">👁 Copy Spectator Link</button>
    {% endif %}
    <button id="flip-btn" class="icon-btn" title="Flip board">⇅ Flip</button>
    <button id="mute-btn" class="icon-btn" title="Mute sounds">🔊</button>
  </div>

  <div id="draw-banner">
    <span id="draw-banner-text"></span>
    <button class="draw-accept" id="draw-accept-btn">Accept</button>
    <button class="draw-decline" id="draw-decline-btn">Decline</button>
  </div>

  <div id="game-area">
    <div id="game-wrap">
      <img id="game" src="data:image/png;base64,{{ first_frame }}" draggable="false"
           {% if spectate %}style="cursor:default;"{% endif %}>
      <canvas id="move-anim"></canvas>
      <canvas id="overlay"></canvas>
    </div>
    <div id="history-panel">
      <div id="history-title">MOVE HISTORY</div>
      <div id="history-list">
        <div class="history-empty" id="history-empty">No moves yet</div>
      </div>
    </div>
  </div>

  <div id="capture-tray">
    <div class="capture-side">
      <span class="capture-label">WHITE CAPTURED</span>
      <span class="capture-pieces" id="capture-white"></span>
      <span class="capture-diff" id="capture-diff-white"></span>
    </div>
    <div class="capture-side">
      <span class="capture-label">BLACK CAPTURED</span>
      <span class="capture-pieces" id="capture-black"></span>
      <span class="capture-diff" id="capture-diff-black"></span>
    </div>
  </div>

  {% if not spectate %}
  <div id="bottom-row">
    {% if not ai_mode %}
    <button id="draw-btn">🤝 Offer Draw</button>
    {% endif %}
    <button id="undo-btn">↺ Undo</button>
    <button id="reset-btn">⟳ Reset Game</button>
    {% if ai_mode %}
    <button id="rematch-btn">🔄 Rematch</button>
    {% endif %}
    <button id="resign-btn">🏳 Resign</button>
  </div>
  {% endif %}

  <div id="promo-overlay">
    <div id="promo-box">
      <h2>PAWN PROMOTION</h2>
      <p>Choose a piece to promote to:</p>
      <div id="promo-buttons"></div>
    </div>
  </div>

  <div id="choice-overlay">
    <div id="choice-box">
      <h2 id="choice-title">CHOOSE A SIDE</h2>
      <p id="choice-subtitle"></p>
      <div>
        <button class="choice-btn" id="choice-white-btn">♔ White</button>
        <button class="choice-btn" id="choice-black-btn">♚ Black</button>
      </div>
      <div><button class="choice-cancel" id="choice-cancel-btn">Cancel</button></div>
    </div>
  </div>

  <script>
    const ROOM = "{{ room_id }}";
    const AI_MODE = {{ 'true' if ai_mode else 'false' }};
    const SPECTATE = {{ 'true' if spectate else 'false' }};

    const themeToggle = document.getElementById('theme-toggle');
    function updateThemeToggle() {
      themeToggle.textContent = document.documentElement.dataset.theme === 'light' ? '☀️' : '🌙';
    }
    updateThemeToggle();
    themeToggle.addEventListener('click', () => {
      const isLight = document.documentElement.dataset.theme === 'light';
      if (isLight) delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = 'light';
      localStorage.setItem('hexchess_theme', isLight ? 'dark' : 'light');
      updateThemeToggle();
    });

    document.getElementById('share-url').textContent = window.location.href;
    document.getElementById('copy-btn').addEventListener('click', function() {
      navigator.clipboard.writeText(window.location.href).then(() => {
        this.textContent = 'Copied!';
        setTimeout(() => this.textContent = 'Copy Link', 1500);
      });
    });

    if (!SPECTATE) {
      const copyWatchBtn = document.getElementById('copy-watch-btn');
      copyWatchBtn.addEventListener('click', function() {
        const watchUrl = window.location.href.replace('/game/', '/watch/');
        navigator.clipboard.writeText(watchUrl).then(() => {
          this.textContent = '👁 Copied!';
          setTimeout(() => this.textContent = '👁 Copy Spectator Link', 1500);
        });
      });
    }

    // ================================================================
    // FLIP BOARD (per-viewer only — never sent to the other player,
    // just changes how this browser fetches/interprets frames)
    // ================================================================
    let flipped = localStorage.getItem('hexchess_flip_'+ROOM) === '1';
    const flipBtn = document.getElementById('flip-btn');
    flipBtn.classList.toggle('active', flipped);
    flipBtn.addEventListener('click', () => {
      flipped = !flipped;
      localStorage.setItem('hexchess_flip_'+ROOM, flipped ? '1' : '0');
      flipBtn.classList.toggle('active', flipped);
      // No need to force a re-fetch — the frame poll below already runs
      // every ~80ms and will pick up the new flip state on its next tick.
    });

    // ================================================================
    // SOUND ENGINE (Web Audio API — fully procedural, no files needed)
    // ================================================================
    let muted = localStorage.getItem('hexchess_muted') === '1';
    const AC = window.AudioContext || window.webkitAudioContext;
    let audioCtx = null;
    function ensureAudio() {
      if (!audioCtx && AC) { try { audioCtx = new AC(); } catch(e){} }
      return audioCtx;
    }
    function tone(freq, type, dur, vol, freqEnd) {
      if (muted) return;
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

    const muteBtn = document.getElementById('mute-btn');
    function updateMuteBtn() {
      muteBtn.textContent = muted ? '🔇' : '🔊';
      muteBtn.classList.toggle('active', muted);
    }
    updateMuteBtn();
    muteBtn.addEventListener('click', () => {
      muted = !muted;
      localStorage.setItem('hexchess_muted', muted ? '1' : '0');
      updateMuteBtn();
    });

    // ================================================================
    // HOVER OVERLAY
    // ================================================================
    const img       = document.getElementById('game');
    const overlay   = document.getElementById('overlay');
    const ctx       = overlay.getContext('2d');
    const moveAnim  = document.getElementById('move-anim');
    const animCtx   = moveAnim.getContext('2d');
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
    function syncOverlay() {
      const r=img.getBoundingClientRect();
      overlay.width=r.width; overlay.height=r.height;
      moveAnim.width=r.width; moveAnim.height=r.height;
    }

    // Per-viewer Flip rotates the board 180 degrees visually without
    // touching the underlying board state — mirror that same negation
    // here so the animated sprite starts/ends on the correct pixels.
    function boardToScreen(q, r) {
      return flipped ? hexToPixel(-q, -r) : hexToPixel(q, r);
    }

    let animFrame = null;
    function animateMove(fromQR, toQR, sym) {
      if (!sym) return;
      if (animFrame) cancelAnimationFrame(animFrame);
      const rect = img.getBoundingClientRect();
      const sx = rect.width / IMG_W, sy = rect.height / IMG_H;
      const start = boardToScreen(fromQR[0], fromQR[1]);
      const end   = boardToScreen(toQR[0], toQR[1]);
      const fontSize = Math.max(10, (start.ts * 1.5) / sy);
      const duration = 180;
      const t0 = performance.now();

      function frame(now) {
        const t = Math.min(1, (now - t0) / duration);
        const ease = 1 - Math.pow(1 - t, 3);
        const x = (start.x + (end.x - start.x) * ease) / sx;
        const y = (start.y + (end.y - start.y) * ease) / sy;

        animCtx.clearRect(0, 0, moveAnim.width, moveAnim.height);
        animCtx.font = fontSize + 'px "Segoe UI", system-ui, sans-serif';
        animCtx.textAlign = 'center';
        animCtx.textBaseline = 'middle';
        animCtx.fillStyle = 'rgba(0,0,0,0.35)';
        animCtx.fillText(sym, x + 1.5, y + 2.5);
        animCtx.fillStyle = '#fff';
        animCtx.fillText(sym, x, y);

        if (t < 1) {
          animFrame = requestAnimationFrame(frame);
        } else {
          animCtx.clearRect(0, 0, moveAnim.width, moveAnim.height);
          animFrame = null;
        }
      }
      animFrame = requestAnimationFrame(frame);
    }
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
    let promoActive=false;
    function refresh() {
      const n=new Image();
      n.onload=()=>{ img.src=n.src; syncOverlay(); checkState(); setTimeout(refresh,80); };
      n.onerror=()=>setTimeout(refresh,300);
      n.src='/frame/'+ROOM+'?t='+Date.now()+'&flip='+(flipped?'1':'0');
    }
    setTimeout(refresh,80);

    // ================================================================
    // STATE POLLING (AI move sounds, promotion detection, move animation)
    // Runs once per frame poll (~80ms) rather than on a separate slower
    // timer, so move-slide detection lags the actual move as little as
    // this polling architecture allows.
    // ================================================================
    let lastEventSeq = -1;
    let lastMoveSeen = null;
    let firstStatePoll = true;
    async function checkState() {
      try {
        const d = await (await fetch('/state/'+ROOM+'?t='+Date.now())).json();
        // Play sound for AI moves (or any server-side event)
        if (d.event_seq !== lastEventSeq && lastEventSeq !== -1) {
          playSound(d.last_event);
        }
        lastEventSeq = d.event_seq;

        // Slide the piece that just moved from its old square to its new
        // one. Keyed off last_move actually changing (not just being
        // present) so resign/draw-agreement — which bump event_seq without
        // moving anything — never replay a stale animation.
        const moveKey = d.last_move ? d.last_move.from.join(',')+'>'+d.last_move.to.join(',') : null;
        if (!firstStatePoll && moveKey && moveKey !== lastMoveSeen) {
          animateMove(d.last_move.from, d.last_move.to, d.last_move.sym);
        }
        lastMoveSeen = moveKey;
        firstStatePoll = false;

        if (AI_MODE) {
          const sideText = document.getElementById('ai-side-text');
          if (sideText && d.ai_color) {
            sideText.textContent = 'you play ' + (d.ai_color === 'white' ? 'Black' : 'White');
          }
        }

        if (SPECTATE) return;  // spectators don't resolve promotions/draws/game-over

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

        // Disable Resign/Offer Draw once the game is over
        document.getElementById('resign-btn').disabled = !!d.winner;
        const drawBtn = document.getElementById('draw-btn');
        if (drawBtn) drawBtn.disabled = !!d.winner;

        // Draw-offer banner (shown to both tabs; either side can respond,
        // same trust model the rest of this shared-room app already uses)
        const banner = document.getElementById('draw-banner');
        if (d.draw_offered_by && !d.winner) {
          banner.classList.add('active');
          document.getElementById('draw-banner-text').textContent =
            d.draw_offered_by.charAt(0).toUpperCase() + d.draw_offered_by.slice(1) + ' offers a draw';
        } else {
          banner.classList.remove('active');
        }
      } catch(e) {}
    }

    async function choosePromo(piece) {
      promoActive=false;
      document.getElementById('promo-overlay').classList.remove('active');
      await fetch('/promote/'+ROOM,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({piece})});
    }

    // ================================================================
    // RESIGN / OFFER DRAW
    // ================================================================
    // Generic "which side?" popup, reused for both Resign and Offer Draw —
    // this app has no player identity/login, so the acting client just
    // states which color it's acting for (same trust model as moves).
    function askColor(title, subtitle, onChoose) {
      document.getElementById('choice-title').textContent = title;
      document.getElementById('choice-subtitle').textContent = subtitle;
      const overlay = document.getElementById('choice-overlay');
      overlay.classList.add('active');
      const cleanup = () => overlay.classList.remove('active');
      document.getElementById('choice-white-btn').onclick = () => { cleanup(); onChoose('white'); };
      document.getElementById('choice-black-btn').onclick = () => { cleanup(); onChoose('black'); };
      document.getElementById('choice-cancel-btn').onclick = cleanup;
    }

    if (!SPECTATE) {
      document.getElementById('resign-btn').addEventListener('click', () => {
        // In vs-AI games there's only one human, always playing White, so
        // there's no ambiguity to ask about (and the server rejects
        // resigning as the AI's color regardless).
        if (AI_MODE) {
          fetch('/resign/'+ROOM, {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({color:'white'})});
          return;
        }
        askColor('RESIGN', 'Which side is resigning?', async (color) => {
          await fetch('/resign/'+ROOM, {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({color})});
        });
      });

      const rematchBtnEl = document.getElementById('rematch-btn');
      if (rematchBtnEl) {
        rematchBtnEl.addEventListener('click', () => {
          fetch('/rematch/'+ROOM, {method:'POST'});
        });
      }
    }

    const drawBtnEl = document.getElementById('draw-btn');
    if (drawBtnEl) {
      drawBtnEl.addEventListener('click', () => {
        askColor('OFFER DRAW', 'Which side is offering the draw?', async (color) => {
          await fetch('/draw_offer/'+ROOM, {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({color})});
        });
      });
    }

    document.getElementById('draw-accept-btn').addEventListener('click', async () => {
      await fetch('/draw_respond/'+ROOM, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({accept:true})});
    });
    document.getElementById('draw-decline-btn').addEventListener('click', async () => {
      await fetch('/draw_respond/'+ROOM, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({accept:false})});
    });

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
      renderCaptureTray(moves);
    }
    function moveHTML(m) {
      const cls=m.color==='white'?'white-entry':'black-entry';
      const cap=m.captured?'<span class="capture-dot"></span>':'';
      const promo=m.promo_to?`<span class="promo-badge">=${m.promo_to[0].toUpperCase()}</span>`:'';
      return `<div class="move-entry ${cls}"><span class="sym">${m.sym}</span><span class="coords">${m.from_label}→${m.to_label}</span>${cap}${promo}</div>`;
    }

    // ================================================================
    // CAPTURED-PIECES TRAY
    // ================================================================
    const CAPTURE_VALUE   = {pawn:1, knight:3, bishop:3, queen:9};
    const CAPTURE_ORDER    = {pawn:0, knight:1, bishop:2, queen:3};
    const WHITE_PIECE_GLYPH = {pawn:'♙', knight:'♘', bishop:'♗', queen:'♕'};
    const BLACK_PIECE_GLYPH = {pawn:'♟', knight:'♞', bishop:'♝', queen:'♛'};

    function renderCaptureTray(moves) {
      // "White captured" = black pieces White has taken, and vice versa —
      // render each with the glyph of the piece that was actually captured.
      const takenByWhite = moves.filter(m => m.color === 'white' && m.captured).map(m => m.captured);
      const takenByBlack = moves.filter(m => m.color === 'black' && m.captured).map(m => m.captured);
      const byValue = (a, b) => (CAPTURE_ORDER[a] ?? 9) - (CAPTURE_ORDER[b] ?? 9);
      takenByWhite.sort(byValue);
      takenByBlack.sort(byValue);

      document.getElementById('capture-white').textContent =
        takenByWhite.map(p => BLACK_PIECE_GLYPH[p] || '').join(' ');
      document.getElementById('capture-black').textContent =
        takenByBlack.map(p => WHITE_PIECE_GLYPH[p] || '').join(' ');

      const sum = list => list.reduce((total, p) => total + (CAPTURE_VALUE[p] || 0), 0);
      const diff = sum(takenByWhite) - sum(takenByBlack);
      document.getElementById('capture-diff-white').textContent = diff > 0 ? '+' + diff : '';
      document.getElementById('capture-diff-black').textContent = diff < 0 ? '+' + (-diff) : '';
    }

    // ================================================================
    // RESET / UNDO
    // ================================================================
    if (!SPECTATE) {
      document.getElementById('reset-btn').addEventListener('click',()=>{
        promoActive=false;
        document.getElementById('promo-overlay').classList.remove('active');
        document.getElementById('draw-banner').classList.remove('active');
        fetch('/reset/'+ROOM,{method:'POST'});
      });

      document.getElementById('undo-btn').addEventListener('click',()=>{
        fetch('/undo/'+ROOM,{method:'POST'});
      });
    }

    // ================================================================
    // CLICK HANDLER
    // ================================================================
    if (!SPECTATE) {
      img.addEventListener('click', async function(e) {
        if(promoActive) return;
        ensureAudio();  // ensure audio context created on user gesture
        const rect=img.getBoundingClientRect();
        const resp=await fetch('/click/'+ROOM,{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({x:e.clientX-rect.left, y:e.clientY-rect.top,
                               imgW:rect.width, imgH:rect.height, flip:flipped})
        });
        const data=await resp.json();
        playSound(data.event);
      });
    }
  </script>
</body>
</html>'''


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

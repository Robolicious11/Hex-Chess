"""Tests for the pure hex-chess rules engine (my_hexchess.Game).

Every test builds `Game(size=4)` explicitly: this matches the board size
server.py always uses in production (BOARD_SIZE = 4). Game()'s size=3
default is inconsistent with setup_pieces()'s hardcoded labels (e.g. "H4"
falls outside a size-3 board), so it isn't a representative configuration
to test against.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_hexchess import Game, Piece

BOARD_SIZE = 4


def fresh_game():
    return Game(size=BOARD_SIZE)


def clear(game):
    for pos in list(game.board.keys()):
        game.board[pos] = None


def lone_piece(name, owner, pos, turn=None):
    game = fresh_game()
    clear(game)
    game.board[pos] = Piece(name, owner)
    game.turn = turn or owner
    return game


# --- Setup ------------------------------------------------------------

def test_initial_board_setup_piece_counts():
    game = fresh_game()
    for owner in ("white", "black"):
        pieces = [p for p in game.board.values() if p and p.owner == owner]
        counts = {}
        for p in pieces:
            counts[p.name] = counts.get(p.name, 0) + 1
        assert counts == {"pawn": 7, "bishop": 2, "king": 1, "queen": 1, "knight": 2}


def test_initial_turn_is_white():
    assert fresh_game().turn == "white"


# --- Piece movement -----------------------------------------------------

def test_king_moves_one_step_each_direction():
    game = lone_piece("king", "white", (0, 0))
    expected = {(1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1), (1, -1)}
    assert set(game.legal_moves((0, 0))) == expected


def test_queen_blocked_by_own_piece_and_can_capture_enemy():
    game = lone_piece("queen", "white", (0, 0))
    game.board[(2, 0)] = Piece("pawn", "white")   # own blocker
    game.board[(-2, 0)] = Piece("pawn", "black")  # enemy blocker
    moves = game.legal_moves((0, 0))
    assert (1, 0) in moves            # empty square before own blocker
    assert (2, 0) not in moves        # can't land on / pass own piece
    assert (-1, 0) in moves           # empty square before enemy blocker
    assert (-2, 0) in moves           # capture enemy blocker
    assert (-3, 0) not in moves       # can't slide past a capture


def test_bishop_restricted_to_diagonal_axes():
    game = lone_piece("bishop", "white", (0, 0))
    moves = set(game.legal_moves((0, 0)))
    # Bishop uses (1,0)/(-1,0) and (1,-1)/(-1,1) — not the (0,1)/(0,-1) axis.
    assert (4, 0) in moves and (-4, 0) in moves
    assert (4, -4) in moves and (-4, 4) in moves
    assert (0, 4) not in moves and (0, -4) not in moves


def test_knight_custom_hex_jump_pattern():
    game = lone_piece("knight", "white", (0, 0))
    expected = {
        (-3, 1), (-3, 2), (-2, -1), (-2, 3), (-1, -2), (-1, 3),
        (1, -3), (1, 2), (2, -3), (2, 1), (3, -2), (3, -1),
    }
    assert set(game.legal_moves((0, 0))) == expected


def test_pawn_double_step_only_before_first_move():
    game = lone_piece("pawn", "white", (0, 3))
    moves = game.legal_moves((0, 3))
    assert (0, 2) in moves and (0, 1) in moves

    game.board[(0, 3)].has_moved = True
    moves_after = game.legal_moves((0, 3))
    assert (0, 2) in moves_after and (0, 1) not in moves_after


def test_pawn_diagonal_capture_requires_enemy_piece():
    game = lone_piece("pawn", "white", (0, 3))
    game.board[(1, 2)] = Piece("pawn", "black")  # capturable
    moves = game.legal_moves((0, 3))
    assert (1, 2) in moves        # enemy present -> legal capture
    assert (-1, 3) not in moves   # empty diagonal, no en passant -> illegal


# --- Check / checkmate / stalemate --------------------------------------

def test_is_in_check_true_when_attacked():
    game = fresh_game()
    clear(game)
    game.board[(0, 0)] = Piece("king", "white")
    game.board[(4, -4)] = Piece("queen", "black")
    assert game.is_in_check("white") is True
    del game.board[(4, -4)]
    game.board[(4, -4)] = None
    assert game.is_in_check("white") is False


def test_legal_moves_excludes_moves_exposing_own_king():
    game = fresh_game()
    clear(game)
    game.board[(0, 0)] = Piece("king", "white")
    game.board[(1, 0)] = Piece("queen", "white")
    game.board[(3, 0)] = Piece("queen", "black")
    game.board[(-4, 4)] = Piece("king", "black")
    game.turn = "white"

    moves = game.legal_moves((1, 0))
    assert (1, 1) not in moves   # would step off the pin line, exposing check
    assert (2, 0) in moves       # stays on the pin line
    assert (3, 0) in moves       # capturing the pinning queen also stays on it


def test_is_checkmate_true_in_mated_position():
    game = fresh_game()
    clear(game)
    game.board[(4, -4)] = Piece("king", "white")
    game.board[(3, -3)] = Piece("queen", "black")
    game.board[(2, -2)] = Piece("bishop", "black")  # defends the queen
    game.board[(-4, 4)] = Piece("king", "black")
    game.turn = "white"

    assert game.is_in_check("white") is True
    assert game.is_checkmate("white") is True
    assert game.is_stalemate("white") is False


def test_is_stalemate_true_with_no_legal_moves_and_no_check():
    game = fresh_game()
    clear(game)
    game.board[(4, -4)] = Piece("king", "white")
    game.board[(2, -3)] = Piece("queen", "black")
    game.board[(-4, 4)] = Piece("king", "black")
    game.turn = "white"

    assert game.is_in_check("white") is False
    assert game.is_stalemate("white") is True
    assert game.is_checkmate("white") is False


# --- En passant -----------------------------------------------------------

def _en_passant_setup():
    game = fresh_game()
    clear(game)
    game.board[(-4, 0)] = Piece("king", "white")
    game.board[(4, -4)] = Piece("king", "black")
    game.board[(0, 2)] = Piece("pawn", "white")
    game.board[(1, 0)] = Piece("pawn", "black")
    game.turn = "white"
    return game


def test_en_passant_capture_removes_pawn_behind_target():
    game = _en_passant_setup()
    game.move((0, 2), (0, 0))
    assert game.en_passant_target == (0, 1)

    black_moves = game.legal_moves((1, 0))
    assert (0, 1) in black_moves

    game.move((1, 0), (0, 1))
    assert game.board[(0, 0)] is None            # captured pawn removed
    assert game.board[(0, 1)].owner == "black"   # capturing pawn relocated


def test_en_passant_expires_after_one_intervening_move():
    game = _en_passant_setup()
    game.move((0, 2), (0, 0))
    assert game.en_passant_target == (0, 1)

    # Black plays something other than the en passant capture.
    game.move((4, -4), (4, -3))
    assert game.en_passant_target is None
    assert (0, 1) not in game.legal_moves((1, 0))


def test_en_passant_illegal_when_it_exposes_check_and_board_is_restored():
    game = fresh_game()
    clear(game)
    game.board[(0, 4)] = Piece("king", "white")
    game.board[(4, 0)] = Piece("queen", "white")
    game.board[(1, 2)] = Piece("pawn", "white")
    game.board[(-3, 0)] = Piece("king", "black")
    game.board[(2, 0)] = Piece("pawn", "black")
    game.turn = "white"

    game.move((1, 2), (1, 0))
    assert game.en_passant_target == (1, 1)
    assert game.is_in_check("black") is False

    moves = game.legal_moves((2, 0))
    assert (1, 1) not in moves          # capturing would expose black's king
    assert (2, 1) in moves              # ordinary forward move still legal

    # legal_moves() must fully restore the board after simulating the capture.
    assert game.board[(1, 0)].name == "pawn" and game.board[(1, 0)].owner == "white"
    assert game.board[(2, 0)].name == "pawn" and game.board[(2, 0)].owner == "black"
    assert game.board[(1, 1)] is None


# --- Draw detection ---------------------------------------------------

def test_fifty_move_rule_triggers_draw():
    game = fresh_game()
    game.halfmove_clock = 99
    assert game.is_draw() is False
    game.halfmove_clock = 100
    assert game.is_draw() is True


def test_threefold_repetition_triggers_draw():
    game = fresh_game()
    wn_pos = next(pos for pos, p in game.board.items() if p and p.owner == "white" and p.name == "knight")
    bn_pos = next(pos for pos, p in game.board.items() if p and p.owner == "black" and p.name == "knight")
    w_dst = game.legal_moves(wn_pos)[0]

    assert game.is_draw() is False
    for i in range(3):
        game.move(wn_pos, w_dst)
        b_dst = game.legal_moves(bn_pos)[0]
        game.move(bn_pos, b_dst)
        assert wn_pos in game.legal_moves(w_dst)
        game.move(w_dst, wn_pos)
        assert bn_pos in game.legal_moves(b_dst)
        game.move(b_dst, bn_pos)
        if i == 0:
            # Initial position (1st occurrence) + this round-trip (2nd) -> not yet a draw.
            assert game.is_draw() is False
        else:
            # A 2nd round-trip brings the starting position back a 3rd time.
            assert game.is_draw() is True
    assert game.is_draw() is True


# --- Promotion / move validation ------------------------------------------

def test_is_promotion_square_detects_far_edge_per_color():
    game = fresh_game()
    assert game.is_promotion_square((0, -4), "white") is True
    assert game.is_promotion_square((0, 0), "white") is False
    assert game.is_promotion_square((0, 4), "black") is True
    assert game.is_promotion_square((0, 0), "black") is False


def test_move_rejects_illegal_destination_without_mutating_state():
    game = fresh_game()
    start = game.from_label("B1")
    illegal_dest = game.from_label("F2")  # white king's own square
    before_board = dict(game.board)
    before_turn = game.turn
    before_clock = game.halfmove_clock

    game.move(start, illegal_dest)

    assert game.board == before_board
    assert game.turn == before_turn
    assert game.halfmove_clock == before_clock


def test_move_resets_halfmove_clock_on_pawn_or_capture_only():
    game = fresh_game()
    wn_pos = next(pos for pos, p in game.board.items() if p and p.owner == "white" and p.name == "knight")
    dst = game.legal_moves(wn_pos)[0]
    game.move(wn_pos, dst)
    assert game.halfmove_clock == 1

    pawn_pos = next(pos for pos, p in game.board.items() if p and p.owner == "black" and p.name == "pawn")
    pawn_dst = game.legal_moves(pawn_pos)[0]
    game.move(pawn_pos, pawn_dst)
    assert game.halfmove_clock == 0


# --- Coordinates ----------------------------------------------------------

def test_label_coordinate_round_trip():
    game = fresh_game()
    for label in ("A1", "D3", "I9", "E5"):
        assert game.to_label(*game.from_label(label)) == label

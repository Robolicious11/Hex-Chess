import os
os.environ.setdefault("DISPLAY", ":1")

import pygame
import math
from my_hexchess import Game

# --- SETTINGS ---
ZOOM = 0.6
DRAW_SCALE = 0.95   # nice tight fit

# --- HEX DRAWING ---
def draw_hex(surface, x, y, size, color):
    points = []

    for i in range(6):
        angle_deg = 60 * i   # correct orientation
        angle_rad = math.radians(angle_deg)

        px = x + size * math.cos(angle_rad)
        py = y + size * math.sin(angle_rad)

        points.append((px, py))

    pygame.draw.polygon(surface, color, points)
    pygame.draw.polygon(surface, (0, 0, 0), points, 1)  # outline

# --- INIT ---
pygame.init()
label_font = pygame.font.SysFont("Arial", 10)
banner_font = pygame.font.SysFont("Arial", 16, bold=True)
big_font = pygame.font.SysFont("Arial", 32, bold=True)
sub_font = pygame.font.SysFont("Arial", 16)

WIDTH, HEIGHT = 700, 580
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Hex Chess")

game = Game(size=4)

selected = None
legal_moves = []
winner = None            # "white" | "black" | "draw" | None
win_reason = None        # "checkmate" | "stalemate" | "draw"
pending_promotion = None # (q, r) of a pawn awaiting a promotion choice

# --- PIECES ---
PIECE_SYMBOLS = {
    "white": {
        "king": "♔",
        "queen": "♕",
        "bishop": "♗",
        "knight": "♘",
        "pawn": "♙"
    },
    "black": {
        "king": "♚",
        "queen": "♛",
        "bishop": "♝",
        "knight": "♞",
        "pawn": "♟"
    }
}

PROMOTION_KEYS = {
    pygame.K_q: "queen",
    pygame.K_b: "bishop",
    pygame.K_n: "knight",
}


def check_game_over():
    cur = game.turn
    if game.is_checkmate(cur):
        return ("black" if cur == "white" else "white"), "checkmate"
    if game.is_stalemate(cur):
        return "draw", "stalemate"
    if game.is_draw():
        return "draw", "draw"
    return None, None


def reset_game():
    global game, selected, legal_moves, winner, win_reason, pending_promotion
    game = Game(size=4)
    selected = None
    legal_moves = []
    winner = None
    win_reason = None
    pending_promotion = None


# --- MAIN LOOP ---
running = True
while running:
    screen.fill((255, 255, 255))

    king_in_check_pos = None
    if not winner and not pending_promotion and game.is_in_check(game.turn):
        for pos, piece in game.board.items():
            if piece and piece.name == "king" and piece.owner == game.turn:
                king_in_check_pos = pos
                break

    for (q, r), piece in game.board.items():
        x, y, tile_size = game.to_pixel(q, r, WIDTH, HEIGHT, zoom=ZOOM)

        hex_radius = int(tile_size * DRAW_SCALE)

        # --- highlight ---
        if selected == (q, r):
            color = (255, 255, 0)
        elif (q, r) in legal_moves:
            color = (150, 255, 150)
        elif (q, r) == king_in_check_pos:
            color = (235, 90, 90)
        else:
            color = (180, 180, 180)

        draw_hex(screen, x, y, hex_radius, color)

        # --- draw piece ---
        if piece:
            piece_font = pygame.font.SysFont("dejavusans", int(hex_radius * 1.5))
            symbol = PIECE_SYMBOLS[piece.owner][piece.name]
            text = piece_font.render(symbol, True, (0, 0, 0))
            rect = text.get_rect(center=(x, y))
            screen.blit(text, rect)

        # LABEL POSITION (BOTTOM CENTER)
        label = game.to_label(q, r)
        text = label_font.render(label, True, (80, 80, 80))

        rect = text.get_rect(center=(x, y + hex_radius * 0.65))
        screen.blit(text, rect)

    # --- status banner ---
    if not winner:
        in_check = king_in_check_pos is not None
        turn_name = "White" if game.turn == "white" else "Black"
        label = f"{turn_name} is in CHECK!" if in_check else f"{turn_name}'s turn"
        bg = (200, 40, 40) if in_check else (235, 235, 235)
        fg = (255, 255, 255) if in_check else (0, 0, 0)
        banner = banner_font.render(label, True, fg)
        pad_w, pad_h = banner.get_width() + 20, banner.get_height() + 12
        pygame.draw.rect(screen, bg, (8, 8, pad_w, pad_h), border_radius=8)
        pygame.draw.rect(screen, (0, 0, 0), (8, 8, pad_w, pad_h), 1, border_radius=8)
        screen.blit(banner, (18, 14))

    # --- promotion prompt ---
    if pending_promotion:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        screen.blit(overlay, (0, 0))
        msg = big_font.render("Promote pawn", True, (255, 255, 255))
        hint = sub_font.render("Press Q (queen), B (bishop), or N (knight)", True, (220, 220, 220))
        screen.blit(msg, msg.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 20)))
        screen.blit(hint, hint.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 20)))

    # --- game over overlay ---
    if winner:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        screen.blit(overlay, (0, 0))
        if winner == "draw":
            text = "Draw — repetition / 50-move rule" if win_reason == "draw" else "Stalemate — Draw!"
        else:
            text = f"{winner.capitalize()} wins by checkmate!"
        msg = big_font.render(text, True, (255, 218, 48))
        hint = sub_font.render("Press R to play again", True, (220, 220, 220))
        screen.blit(msg, msg.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 20)))
        screen.blit(hint, hint.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 20)))

    pygame.display.flip()

    # --- EVENTS ---
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        elif event.type == pygame.KEYDOWN:
            if pending_promotion and event.key in PROMOTION_KEYS:
                pos = pending_promotion
                game.board[pos].name = PROMOTION_KEYS[event.key]
                pending_promotion = None
                game.en_passant_target = None
                game.halfmove_clock = 0
                game.turn = "black" if game.turn == "white" else "white"
                game.record_position()
                winner, win_reason = check_game_over()
            elif winner and event.key == pygame.K_r:
                reset_game()

        elif event.type == pygame.MOUSEBUTTONDOWN:
            if winner or pending_promotion:
                continue

            mx, my = pygame.mouse.get_pos()

            q, r = game.from_pixel(mx, my, WIDTH, HEIGHT, zoom=ZOOM)

            if (q, r) not in game.board:
                continue

            piece = game.board.get((q, r))

            if selected is None:
                if piece and piece.owner == game.turn:
                    selected = (q, r)
                    legal_moves = game.legal_moves(selected)
            else:
                if (q, r) in legal_moves:
                    moving_piece = game.board[selected]
                    is_promo = (moving_piece.name == "pawn"
                                and game.is_promotion_square((q, r), moving_piece.owner))
                    if is_promo:
                        captured = game.board[(q, r)]
                        game.board[(q, r)] = moving_piece
                        game.board[selected] = None
                        moving_piece.has_moved = True
                        pending_promotion = (q, r)
                    else:
                        game.move(selected, (q, r))
                        winner, win_reason = check_game_over()

                selected = None
                legal_moves = []

pygame.quit()

# Simple customizable hex chess structure

class Piece:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self.has_moved = False

    def __repr__(self):
        return f"{self.owner[0]}-{self.name[0]}"


class Game:
    def __init__(self, size=3):
        self.size = size
        self.board = {}
        self.turn = "white"
        self.en_passant_target = None
        self.halfmove_clock = 0
        self.position_history = []

        self.create_board()
        self.setup_pieces()
        self.record_position()

    # --- Create hex board ---
    def create_board(self):
        for q in range(-self.size, self.size + 1):
            for r in range(-self.size, self.size + 1):
                if abs(q + r) <= self.size:
                    self.board[(q, r)] = None

    # --- Setup pieces ---
    def setup_pieces(self):
        white_setup = [
            ("pawn", "B1"), ("pawn", "C2"), ("pawn", "D3"),
            ("pawn", "E4"), ("pawn", "F4"), ("pawn", "G4"), ("pawn", "H4"),
            ("bishop", "E2"), ("bishop", "E3"),
            ("king", "F2"),
            ("queen", "D1"),
            ("knight", "D2"), ("knight", "F3")
        ]

        for piece_type, label in white_setup:
            pos = self.from_label(label)
            self.board[pos] = Piece(piece_type, "white")

        for piece_type, label in white_setup:
            q, r = self.from_label(label)
            self.board[(-q, -r)] = Piece(piece_type, "black")

    # --- Pseudo-legal moves (no check filtering) ---
    def _pseudo_moves(self, position):
        piece = self.board.get(position)
        if not piece:
            return []

        q, r = position
        moves = []

        directions = [
            (1, 0), (0, 1), (-1, 1),
            (-1, 0), (0, -1), (1, -1)
        ]

        # --- KING ---
        if piece.name == "king":
            for dq, dr in directions:
                pos = (q + dq, r + dr)
                if pos in self.board:
                    target = self.board[pos]
                    if target is None or target.owner != piece.owner:
                        moves.append(pos)

        # --- QUEEN ---
        elif piece.name == "queen":
            for dq, dr in directions:
                steps = 1
                while True:
                    pos = (q + dq * steps, r + dr * steps)
                    if pos not in self.board:
                        break
                    target = self.board[pos]
                    if target is None:
                        moves.append(pos)
                    else:
                        if target.owner != piece.owner:
                            moves.append(pos)
                        break
                    steps += 1

        # --- BISHOP ---
        elif piece.name == "bishop":
            bishop_dirs = [
                (1, 0), (-1, 0),
                (1, -1), (-1, 1)
            ]
            for dq, dr in bishop_dirs:
                steps = 1
                while True:
                    pos = (q + dq * steps, r + dr * steps)
                    if pos not in self.board:
                        break
                    target = self.board[pos]
                    if target is None:
                        moves.append(pos)
                    else:
                        if target.owner != piece.owner:
                            moves.append(pos)
                        break
                    steps += 1

        # --- KNIGHT ---
        elif piece.name == "knight":
            straight_dirs = [
                (1, 0), (-1, 0),
                (0, 1), (0, -1),
                (1, -1), (-1, 1)
            ]

            def hex_dist(q1, r1, q2, r2):
                return abs(q1 - q2) + abs(r1 - r2) + abs((q1 + r1) - (q2 + r2))

            for dq1, dr1 in straight_dirs:
                mid_q = q + 2 * dq1
                mid_r = r + 2 * dr1

                if (mid_q, mid_r) not in self.board:
                    continue

                if dr1 == 0:
                    final_dirs = [(0, 1), (0, -1), (1, -1), (-1, 1)]
                elif dq1 == 0:
                    final_dirs = [(1, 0), (-1, 0), (1, -1), (-1, 1)]
                else:
                    final_dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]

                for dq2, dr2 in final_dirs:
                    new_q = mid_q + dq2
                    new_r = mid_r + dr2

                    if (new_q, new_r) not in self.board:
                        continue

                    if hex_dist(new_q, new_r, q, r) <= hex_dist(mid_q, mid_r, q, r):
                        continue

                    target = self.board[(new_q, new_r)]
                    if target is None or target.owner != piece.owner:
                        moves.append((new_q, new_r))

        # --- PAWN ---
        elif piece.name == "pawn":
            forward = (0, -1) if piece.owner == "white" else (0, 1)

            one = (q + forward[0], r + forward[1])
            if one in self.board and self.board[one] is None:
                moves.append(one)
                if not piece.has_moved:
                    two = (q + 2 * forward[0], r + 2 * forward[1])
                    if two in self.board and self.board[two] is None:
                        moves.append(two)

            captures = [(1, -1), (-1, 0)] if piece.owner == "white" else [(1, 0), (-1, 1)]
            for dq, dr in captures:
                pos = (q + dq, r + dr)
                if pos in self.board:
                    target = self.board[pos]
                    if target and target.owner != piece.owner:
                        moves.append(pos)
                    elif pos == self.en_passant_target:
                        moves.append(pos)

        return moves

    # --- Check detection ---
    def is_in_check(self, color):
        king_pos = None
        for pos, piece in self.board.items():
            if piece and piece.name == "king" and piece.owner == color:
                king_pos = pos
                break
        if king_pos is None:
            return False

        opponent = "black" if color == "white" else "white"
        for pos, piece in self.board.items():
            if piece and piece.owner == opponent:
                if king_pos in self._pseudo_moves(pos):
                    return True
        return False

    # --- Legal moves (pseudo-moves filtered to not leave own king in check) ---
    def legal_moves(self, position):
        piece = self.board.get(position)
        if not piece or piece.owner != self.turn:
            return []

        legal = []
        for end in self._pseudo_moves(position):
            captured = self.board[end]

            # En passant: the captured pawn sits behind the destination square,
            # not on it, so it must be removed separately for the check test.
            ep_pos = None
            ep_captured = None
            if piece.name == "pawn" and end == self.en_passant_target and captured is None:
                forward = (0, -1) if piece.owner == "white" else (0, 1)
                ep_pos = (end[0] - forward[0], end[1] - forward[1])
                ep_captured = self.board.get(ep_pos)
                self.board[ep_pos] = None

            self.board[end] = piece
            self.board[position] = None

            still_in_check = self.is_in_check(piece.owner)

            self.board[position] = piece
            self.board[end] = captured
            if ep_pos is not None:
                self.board[ep_pos] = ep_captured

            if not still_in_check:
                legal.append(end)

        return legal

    # --- Check if a color has any legal move available ---
    def has_legal_moves(self, color):
        orig_turn = self.turn
        self.turn = color
        for pos, piece in self.board.items():
            if piece and piece.owner == color:
                if self.legal_moves(pos):
                    self.turn = orig_turn
                    return True
        self.turn = orig_turn
        return False

    # --- Checkmate / stalemate ---
    def is_checkmate(self, color):
        return self.is_in_check(color) and not self.has_legal_moves(color)

    def is_stalemate(self, color):
        return not self.is_in_check(color) and not self.has_legal_moves(color)

    # --- Draw detection (50-move rule / threefold repetition) ---
    def _position_key(self):
        pieces = tuple(sorted(
            (pos, piece.name, piece.owner)
            for pos, piece in self.board.items() if piece
        ))
        return (pieces, self.turn, self.en_passant_target)

    def record_position(self):
        """Call after any move (including externally-applied ones like
        promotions) so repetition/50-move tracking stays accurate."""
        self.position_history.append(self._position_key())

    def is_draw(self):
        if self.halfmove_clock >= 100:
            return True
        if self.position_history and self.position_history.count(self.position_history[-1]) >= 3:
            return True
        return False

    # --- Pawn promotion ---
    def is_promotion_square(self, pos, owner):
        """True if a pawn at pos has reached the far edge for that owner."""
        q, r = pos
        forward_r = -1 if owner == "white" else 1
        return (q, r + forward_r) not in self.board

    # --- Move ---
    def move(self, start, end):
        piece = self.board[start]

        if end not in self.legal_moves(start):
            print("Illegal move!")
            return

        captured = self.board[end]

        # En passant capture: remove the pawn that just double-stepped.
        if piece.name == "pawn" and end == self.en_passant_target and captured is None:
            forward = (0, -1) if piece.owner == "white" else (0, 1)
            ep_pos = (end[0] - forward[0], end[1] - forward[1])
            captured = self.board.get(ep_pos)
            self.board[ep_pos] = None

        self.board[end] = piece
        self.board[start] = None
        piece.has_moved = True

        # New en passant target if this was a pawn double-step.
        new_ep = None
        if piece.name == "pawn" and start[0] == end[0] and abs(start[1] - end[1]) == 2:
            new_ep = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
        self.en_passant_target = new_ep

        if piece.name == "pawn" or captured is not None:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        self.turn = "black" if self.turn == "white" else "white"
        self.record_position()

    # --- Pixel conversion ---
    def to_pixel(self, q, r, width, height, zoom=1.0):
        base = min(width, height) // (2 * self.size + 2)
        tile_size = int(base * zoom)

        x = width // 2 + tile_size * (1.5 * q)
        y = height // 2 + tile_size * (0.866 * (2 * r + q))

        return int(x), int(y), tile_size

    def from_pixel(self, x, y, width, height, zoom=1.0):
        base = min(width, height) // (2 * self.size + 2)
        tile_size = int(base * zoom)

        x0 = x - width // 2
        y0 = y - height // 2

        q = (2/3) * x0 / tile_size
        r = ((y0 / (tile_size * 0.866)) - q) / 2

        return self.hex_round(q, r)

    def hex_round(self, q, r):
        s = -q - r

        rq = round(q)
        rr = round(r)
        rs = round(s)

        dq = abs(rq - q)
        dr = abs(rr - r)
        ds = abs(rs - s)

        if dq > dr and dq > ds:
            rq = -rr - rs
        elif dr > ds:
            rr = -rq - rs
        else:
            rs = -rq - rr

        return int(rq), int(rr)

    def to_label(self, q, r):
        col_index = q + self.size
        letter = chr(ord('A') + col_index)
        number = -r + self.size + 1
        return f"{letter}{number}"

    def from_label(self, label):
        letter = label[0]
        number = int(label[1:])

        col_index = ord(letter) - ord('A')
        q = col_index - self.size
        r = self.size + 1 - number

        return (q, r)

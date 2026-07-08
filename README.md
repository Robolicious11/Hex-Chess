# Hex Chess

Chess adapted to a hexagonal board — a hex grid instead of a square grid changes how
every piece moves, most notably the knight, which uses a custom "two-step-then-turn"
hex adaptation instead of the familiar orthogonal L-shape.

## Features

- Full legal-move generation for king, queen, bishop, knight, and pawn on a hex board,
  including check filtering (a move that would expose your own king is never legal).
- Checkmate and stalemate detection.
- En passant, including the "pinned pawn" edge case where taking en passant would
  expose your own king to check.
- Draw detection: the 50-move rule and threefold repetition.
- Pawn promotion.
- Two ways to play: a local desktop client and a browser client (see below).
- An AI opponent with three difficulty levels (random / greedy / iterative-deepening
  alpha-beta search).
- Per-player chess clocks and a move history panel (browser client).

## Two ways to play

### Local desktop client

```
python game.py
```

Opens a real window via `pygame`. Click a piece to select it, click a highlighted
square to move. When a pawn promotes, press **Q**, **B**, or **N** to choose the piece.
Press **R** to start a new game once the game-over overlay appears.

### Browser client

```
python server.py
```

Then open `http://localhost:5000`. Pick 2-player or vs-AI mode and a time control, and
share the room URL with a friend for 2-player games.

The browser client renders the board server-side (via `pygame`, to a PNG) and the page
polls for a new frame roughly every 80ms — there's no websocket or canvas rendering
involved. This keeps the server simple, but it also means it isn't a "real-time"
architecture; expect polling-driven latency rather than instant updates.

## Local development setup

```
git clone https://github.com/Robolicious11/Hex-Chess.git
cd Hex-Chess
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`runtime.txt` pins the deployment target to `python-3.11.9`. This has also been
developed and tested against Python 3.13.7 with Flask 3.1.3 and pygame 2.6.1 — if your
local Python differs from `runtime.txt`, that's expected and fine for local dev.

## Deployment

A `Procfile` is included for platforms that support it (Heroku-style buildpacks,
Render, Railway, etc.):

```
web: gunicorn -w 1 --worker-class gthread --threads 8 --bind 0.0.0.0:$PORT server:app
```

**The worker count must stay at 1.** Room state (`rooms` in `server.py`) lives in a
plain in-process Python dict with no shared backing store (no Redis, no database). A
second gunicorn worker process would have its own independent copy of that dict, and
players would randomly get 404s depending on which worker happened to serve their
request. `--worker-class gthread --threads 8` restores the concurrent-request handling
the app already relies on (the existing `render_lock`/per-room locks and the background
AI-move thread are designed around handling multiple requests at once within a single
process).

Because there's no persistence layer, a background thread periodically purges rooms
that have been inactive for more than 2 hours, so the process has bounded memory growth
under public traffic instead of accumulating abandoned rooms forever. Room creation
(`/new`) is also rate-limited per client IP (10 per 10 minutes) to stop a rapid burst of
requests from spiking memory faster than that cleanup sweep runs.

`server.py` reads the `PORT` environment variable when run directly
(`python server.py`); under gunicorn, the Procfile's `--bind` is what actually
determines the port. Note also that `DejaVuSans.ttf` is loaded via a relative path at
import time, so the process must be started from the repo root (true by default for
standard buildpack/Procfile-based hosts).

## Controls / how to play

Squares are labeled like standard chess (e.g. `D3`), just on a hexagonal grid. Both
clients are mouse-driven: click to select a piece, click a highlighted square to move
there. The desktop client uses keyboard shortcuts for promotion (Q/B/N) and reset (R);
the browser client uses on-page buttons for promotion and a "Reset Game" button.

## Testing

```
pip install -r requirements-dev.txt
pytest
```

The test suite covers `my_hexchess.py`'s rules engine: movement per piece type, check/
checkmate/stalemate detection, en passant (including the discovered-check edge case),
and draw detection. `game.py` (the desktop client) opens a real display window at
import time, so it isn't imported in tests or CI — it's covered by a compile check only.

## Project structure

```
my_hexchess.py   Pure-Python hex chess rules engine (no external dependencies)
game.py          Local desktop client (pygame)
server.py        Browser client + AI opponent (Flask + pygame, rendered to PNG)
tests/           pytest suite for my_hexchess.py
DejaVuSans.ttf   Bundled font (see Credits)
```

## Known limitations

This is a personal hobby project, not a production chess platform:

- Single-process only (see Deployment above) — it does not horizontally scale.
- No accounts, no persistence — rooms and their history disappear when the process
  restarts or a room is reaped for inactivity.
- The browser client's polling-based rendering is simple but not low-latency.

## Credits

Piece glyphs and on-board text are rendered with the bundled
[DejaVu Sans](https://dejavu-fonts.github.io/) font, included here because deployment
hosts aren't guaranteed to have system fonts installed. DejaVu Fonts are released under
a permissive license derived from the Bitstream Vera Fonts License — see
https://dejavu-fonts.github.io/License.html for the full terms.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

This started as a personal project, so there's no formal process — issues and pull
requests are welcome.

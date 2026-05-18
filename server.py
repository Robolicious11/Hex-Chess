import os
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

import pygame, math, io, time, threading, uuid, random
from flask import Flask, Response, request, jsonify, render_template_string, redirect
from my_hexchess import Game

app = Flask(__name__)

WIDTH, HEIGHT = 800, 600
ZOOM = 0.6
DRAW_SCALE = 0.95
FONT_PATH = "DejaVuSans.ttf"

pygame.init()
surface = pygame.Surface((WIDTH, HEIGHT))

label_font = pygame.font.Font(FONT_PATH, 14)
_piece_font_cache = {}
rooms = {}
render_lock = threading.Lock()

PIECE_SYMBOLS = {
    "white": {"king":"♔","queen":"♕","bishop":"♗","knight":"♘","pawn":"♙"},
    "black": {"king":"♚","queen":"♛","bishop":"♝","knight":"♞","pawn":"♟"}
}

# -------- ROOM --------
def make_room():
    return {
        "game": Game(size=4),
        "selected": None,
        "legal_moves": [],
        "last_move": None,
        "animation": None,
        "players": {"white": None, "black": None},
        "history": []
    }

def font(size):
    if size not in _piece_font_cache:
        _piece_font_cache[size] = pygame.font.Font(FONT_PATH, size)
    return _piece_font_cache[size]

# -------- DRAW --------
def draw_hex(x, y, size, color):
    pts = [(x + size*math.cos(math.radians(60*i)),
            y + size*math.sin(math.radians(60*i))) for i in range(6)]
    pygame.draw.polygon(surface, color, pts)
    pygame.draw.polygon(surface, (0,0,0), pts, 1)

def render(room):
    g = room["game"]

    surface.fill((245,245,245))

    for (q,r), piece in g.board.items():
        x,y,t = g.to_pixel(q,r,WIDTH,HEIGHT,zoom=ZOOM)
        size = int(t*DRAW_SCALE)

        base = (200,200,200) if (q+r)%2==0 else (160,160,160)
        col = base

        if room["last_move"] and (q,r) in room["last_move"]:
            col = (180,180,255)
        if room["selected"] == (q,r):
            col = (255,230,80)
        elif (q,r) in room["legal_moves"]:
            col = (140,240,140)

        draw_hex(x,y,size,col)

        if piece:
            pf = font(int(size*1.3))
            sym = PIECE_SYMBOLS[piece.owner][piece.name]
            txt = pf.render(sym, True, (0,0,0))
            surface.blit(txt, txt.get_rect(center=(round(x),round(y))))

        label = label_font.render(g.to_label(q,r), False, (70,70,70))
        surface.blit(label, label.get_rect(center=(round(x), round(y+size*0.65))))

    # animation
    anim = room["animation"]
    if anim:
        t = (time.time()-anim["start"]) / 0.2

        if t >= 1:
            g.board[anim["to"]] = anim["piece"]
            room["animation"] = None
        else:
            fx,fy,_ = g.to_pixel(*anim["from"],WIDTH,HEIGHT,zoom=ZOOM)
            tx,ty,_ = g.to_pixel(*anim["to"],WIDTH,HEIGHT,zoom=ZOOM)
            nx = fx + (tx-fx)*t
            ny = fy + (ty-fy)*t

            pf = font(50)
            sym = PIECE_SYMBOLS[anim["piece"].owner][anim["piece"].name]
            surface.blit(pf.render(sym, True, (0,0,0)), (nx-20, ny-20))

def frame_bytes(room):
    with render_lock:
        render(room)
        buf = io.BytesIO()
        pygame.image.save(surface, buf, "png")
        return buf.getvalue()

# -------- ROUTES --------

# ✅ FIXED HOME PAGE
@app.route('/')
def home():
    return render_template_string("""
    <style>
    body{background:#1a1a2e;color:white;text-align:center;font-family:sans-serif;}
    button{padding:14px 24px;background:#3498db;border:none;border-radius:8px;font-size:16px;}
    button:hover{background:#2980b9;}
    </style>

    <h1>HEX CHESS</h1>

    <form action="/new" method="POST">
        <button type="submit">Create Game</button>
    </form>
    """)

@app.route('/new', methods=['POST'])
def new_game():
    rid = uuid.uuid4().hex[:8]
    rooms[rid] = make_room()
    return redirect(f"/game/{rid}")

@app.route('/game/<rid>')
def game_page(rid):
    return render_template_string("""
    <style>
    body{background:#1a1a2e;color:white;text-align:center;}
    #board{border-radius:10px;box-shadow:0 0 40px rgba(0,0,0,.6);}
    #history{height:160px;overflow:auto;background:#16213e;margin-top:10px;padding:10px;border-radius:6px;}
    </style>

    <h2>HEX CHESS</h2>

    <img id="board">

    <h3>Moves</h3>
    <div id="history"></div>

    <script>
    const ROOM="{{rid}";
    const img=document.getElementById("board");
    const hist=document.getElementById("history");

    function refresh(){
        img.src="/frame/"+ROOM+"?t="+Date.now();
        fetch("/state/"+ROOM).then(r=>r.json()).then(d=>{
            hist.innerHTML=d.history.join("<br>");
        });
        setTimeout(refresh,120);
    }
    refresh();

    let sx, sy;

    img.onmousedown=e=>{
        let r=img.getBoundingClientRect();
        sx=e.clientX-r.left; sy=e.clientY-r.top;
    };

    img.onmouseup=e=>{
        let r=img.getBoundingClientRect();
        fetch("/drag/"+ROOM,{
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
                x1:sx,y1:sy,
                x2:e.clientX-r.left,y2:e.clientY-r.top
            })
        });
    };
    </script>
    """, rid=rid)

@app.route('/frame/<rid>')
def frame(rid):
    return Response(frame_bytes(rooms[rid]), mimetype='image/png')

@app.route('/state/<rid>')
def state(rid):
    return jsonify({"history": rooms[rid]["history"]})

@app.route('/drag/<rid>', methods=['POST'])
def drag(rid):
    room = rooms[rid]
    game = room["game"]

    d = request.json
    start = game.from_pixel(d["x1"], d["y1"], WIDTH, HEIGHT, zoom=ZOOM)
    end   = game.from_pixel(d["x2"], d["y2"], WIDTH, HEIGHT, zoom=ZOOM)

    if start not in game.board:
        return jsonify({"ok":True})

    piece = game.board.get(start)

    if room["selected"] is None:
        if piece and piece.owner == game.turn:
            room["selected"]=start
            room["legal_moves"]=game.legal_moves(start)
    else:
        if end in room["legal_moves"]:
            src = room["selected"]

            room["animation"]={
                "from":src,
                "to":end,
                "piece":game.board[src],
                "start":time.time()
            }

            room["history"].append(f"{game.to_label(*src)}→{game.to_label(*end)}")

            game.board[src]=None
            game.turn="black" if game.turn=="white" else "white"
            room["last_move"]=(src,end)

        room["selected"]=None
        room["legal_moves"]=[]

    return jsonify({"ok":True})

# -------- RUN --------
if __name__ == "__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port)
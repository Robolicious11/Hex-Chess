import os
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

import pygame, math, io, time, threading, uuid, base64
from flask import Flask, Response, request, jsonify, render_template_string, redirect
from my_hexchess import Game

app = Flask(__name__)

WIDTH, HEIGHT = 800, 600
ZOOM = 0.6
DRAW_SCALE = 0.95
FONT_PATH = "DejaVuSans.ttf"

pygame.init()
surface = pygame.Surface((WIDTH, HEIGHT))

label_font = pygame.font.Font(FONT_PATH, 13)
_piece_font_cache = {}

rooms = {}
render_lock = threading.Lock()

PIECE_SYMBOLS = {
    "white": {"king": "♔","queen": "♕","bishop": "♗","knight": "♘","pawn": "♙"},
    "black": {"king": "♚","queen": "♛","bishop": "♝","knight": "♞","pawn": "♟"}
}


# ---------- ROOM ----------

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


# ---------- DRAW ----------

def draw_hex(x, y, size, color):
    pts = [(x + size*math.cos(math.radians(60*i)),
            y + size*math.sin(math.radians(60*i))) for i in range(6)]
    pygame.draw.polygon(surface, color, pts)
    pygame.draw.polygon(surface, (0,0,0), pts, 1)


def render(room):
    game = room["game"]
    selected = room["selected"]
    legal = room["legal_moves"]
    last = room["last_move"]

    surface.fill((245,245,245))

    for (q,r), piece in game.board.items():
        x,y,s = game.to_pixel(q,r,WIDTH,HEIGHT,zoom=ZOOM)
        size = int(s*DRAW_SCALE)

        base = (200,200,200) if (q+r)%2==0 else (160,160,160)
        col = base

        if last and (q,r) in last:
            col = (180,180,255)
        if selected==(q,r): col=(255,230,80)
        elif (q,r) in legal: col=(140,240,140)

        draw_hex(x,y,size,col)

        # piece
        if piece:
            pf = font(int(size*1.3))
            sym = PIECE_SYMBOLS[piece.owner][piece.name]
            t = pf.render(sym, True, (20,20,20))
            surface.blit(t, t.get_rect(center=(round(x),round(y))))

        # label
        text = label_font.render(game.to_label(q,r), False, (80,80,80))
        surface.blit(text, text.get_rect(center=(round(x), round(y+size*0.65))))

    # ---- animation ----
    anim = room["animation"]
    if anim:
        t = (time.time()-anim["start"])/0.2
        if t>=1:
            game.board[anim["to"]] = anim["piece"]
            room["animation"]=None
        else:
            fx,fy,_=game.to_pixel(*anim["from"],WIDTH,HEIGHT,zoom=ZOOM)
            tx,ty,_=game.to_pixel(*anim["to"],WIDTH,HEIGHT,zoom=ZOOM)
            x = fx+(tx-fx)*t
            y = fy+(ty-fy)*t

            pf = font(50)
            sym = PIECE_SYMBOLS[anim["piece"].owner][anim["piece"].name]
            t_surf = pf.render(sym, True, (0,0,0))
            surface.blit(t_surf, t_surf.get_rect(center=(x,y)))

    # ---- turn display ----
    turn = game.turn.upper()
    turn_text = label_font.render(f"{turn} TURN", False, (20,20,20))
    surface.blit(turn_text, (10,10))


def frame_bytes(room):
    with render_lock:
        render(room)
        buf=io.BytesIO()
        pygame.image.save(surface, buf, "png")
        return buf.getvalue()


# ---------- ROUTES ----------

@app.route('/')
def home():
    return render_template_string("""
    <style>
    body {background:#1a1a2e;color:white;text-align:center;font-family:sans-serif;}
    button {padding:12px 20px;background:#3498db;border:none;border-radius:8px;color:white;}
    </style>
    <h1>HEX CHESS</h1>
    /new method="POST">
        <button>Create Game</button>
    </form>
    """)


@app.route('/new', methods=['POST'])
def new():
    rid = uuid.uuid4().hex[:8]
    rooms[rid]=make_room()
    return redirect(f"/game/{rid}")


@app.route('/game/<rid>')
def game_page(rid):
    return render_template_string("""
    <style>
    body {background:#1a1a2e;color:white;text-align:center;}
    #board {box-shadow:0 0 40px rgba(0,0,0,0.7);}
    #history {height:150px;overflow:auto;background:#16213e;margin-top:10px;padding:10px;border-radius:6px;}
    </style>

    <h2>HEX CHESS</h2>
    <img id="board">

    <div id="role"></div>

    <h3>Moves</h3>
    <div id="history"></div>

    <script>
    const ROOM="{{rid}}"
    const img=document.getElementById("board")
    const historyDiv=document.getElementById("history")

    function refresh(){
        img.src="/frame/"+ROOM+"?t="+Date.now()

        fetch("/state/"+ROOM)
        .then(r=>r.json())
        .then(d=>{
            historyDiv.innerHTML=d.history.join("<br>")
        })

        setTimeout(refresh,120)
    }

    refresh()

    img.onclick=(e)=>{
        let r=img.getBoundingClientRect()
        fetch("/click/"+ROOM,{
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
                x:e.clientX-r.left,
                y:e.clientY-r.top,
                imgW:r.width,
                imgH:r.height
            })
        })
    }
    </script>
    """,rid=rid)


@app.route('/frame/<rid>')
def frame(rid):
    return Response(frame_bytes(rooms[rid]), mimetype='image/png')


@app.route('/state/<rid>')
def state(rid):
    room=rooms[rid]
    return jsonify({"history":room["history"]})


@app.route('/click/<rid>', methods=['POST'])
def click(rid):
    room=rooms[rid]
    g=room["game"]

    user=request.remote_addr

    if room["players"]["white"] is None:
        room["players"]["white"]=user
    elif room["players"]["black"] is None and user!=room["players"]["white"]:
        room["players"]["black"]=user

    if room["players"][g.turn]!=user:
        return jsonify({"ok":False})

    d=request.json
    mx,my=d["x"],d["y"]

    q,r=g.from_pixel(mx,my,WIDTH,HEIGHT,zoom=ZOOM)

    if (q,r) not in g.board:
        room["selected"]=None
        room["legal_moves"]=[]
        return jsonify({"ok":True})

    p=g.board.get((q,r))

    if room["selected"] is None:
        if p and p.owner==g.turn:
            room["selected"]=(q,r)
            room["legal_moves"]=g.legal_moves((q,r))
    else:
        if (q,r) in room["legal_moves"]:
            src=room["selected"]

            room["animation"]={
                "from":src,
                "to":(q,r),
                "piece":g.board[src],
                "start":time.time()
            }

            move=f"{g.to_label(*src)}→{g.to_label(q,r)}"
            room["history"].append(move)

            g.board[src]=None
            g.turn="black" if g.turn=="white" else "white"
            room["last_move"]=(src,(q,r))

        room["selected"]=None
        room["legal_moves"]=[]

    return jsonify({"ok":True})


# ---------- RUN ----------
if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port)
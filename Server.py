
import asyncio
import hmac
import html
import json
import os
import time
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import (
    FastAPI,
    Request,
    WebSocket,
    WebSocketDisconnect,
    HTTPException
)
from fastapi.responses import HTMLResponse, Response


# ============================================================
# CONFIG
# ============================================================

HOST = "0.0.0.0"
PORT = 5000

PASSWORD = os.environ.get("ARCHIVE_SERVER_PASSWORD", "1986")

CLIENT_TIMEOUT = 35
MAX_FRAME_SIZE = 32 * 1024 * 1024

CLIENTS = {}
CLIENT_LOCK = threading.Lock()


# ============================================================
# AUTH
# ============================================================

def check_password(password):
    return (
        isinstance(password, str)
        and hmac.compare_digest(
            password.encode(),
            PASSWORD.encode()
        )
    )


def authorize_request(request: Request):
    scheme, sep, password = request.headers.get(
        "Authorization", ""
    ).partition(" ")

    if (
        not sep
        or scheme.lower() != "bearer"
        or not check_password(password)
    ):
        raise HTTPException(401, "Invalid password")


# ============================================================
# APP
# ============================================================

@asynccontextmanager
async def lifespan(app):
    print("[ARCHIVE] Server started")
    print(f"[ARCHIVE] Port: {PORT}")
    yield
    print("[ARCHIVE] Server stopped")


app = FastAPI(lifespan=lifespan)


# ============================================================
# REGISTER CLIENT
# ============================================================

@app.post("/api/register")
async def register(request: Request):

    authorize_request(request)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    client_id = str(
        data.get("client_id", "")
    ).strip()

    nickname = str(
        data.get("nickname", "Player")
    ).strip()[:32] or "Player"

    if not client_id or len(client_id) > 64:
        raise HTTPException(400, "Invalid client ID")

    now = time.time()

    with CLIENT_LOCK:

        client = CLIENTS.get(client_id)

        if client is None:

            CLIENTS[client_id] = {
                "id": client_id,
                "nickname": nickname,
                "frame": None,
                "last_seen": now,
                "fps": 0.0,
                "frame_count": 0,
                "fps_start": now,
                "total_frames": 0
            }

        else:

            client["nickname"] = nickname
            client["last_seen"] = now

    print(
        f"[ARCHIVE] Игрок зарегистрирован: "
        f"{nickname} ({client_id})"
    )

    return {
        "status": "ok",
        "client_id": client_id
    }


# ============================================================
# WEBSOCKET STREAM
# ============================================================

@app.websocket("/ws/{client_id}")
async def screen_stream(
    websocket: WebSocket,
    client_id: str
):

    await websocket.accept()

    print(
        f"[ARCHIVE] WebSocket подключение: {client_id}"
    )

    try:

        password = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=10
        )

        if not check_password(password):

            await websocket.close(
                code=1008,
                reason="Wrong password"
            )

            return

        with CLIENT_LOCK:

            if client_id not in CLIENTS:

                await websocket.close(
                    code=1008,
                    reason="Minecraft user not registered"
                )

                return

            CLIENTS[client_id]["last_seen"] = time.time()

        await websocket.send_text("AUTH_OK")

        print(
            f"[ARCHIVE] Авторизация успешна: {client_id}"
        )

        while True:

            frame = await websocket.receive_bytes()

            if not frame or len(frame) > MAX_FRAME_SIZE:
                continue

            if (
                len(frame) < 12
                or frame[:4] != b"RIFF"
                or frame[8:12] != b"WEBP"
            ):
                continue

            now = time.time()

            with CLIENT_LOCK:

                client = CLIENTS.get(client_id)

                if client is None:
                    break

                client["frame"] = frame
                client["last_seen"] = now

                client["frame_count"] += 1
                client["total_frames"] += 1

                elapsed = now - client["fps_start"]

                if elapsed >= 1:

                    client["fps"] = (
                        client["frame_count"] / elapsed
                    )

                    client["frame_count"] = 0
                    client["fps_start"] = now

                total = client["total_frames"]

            if total == 1 or total % 150 == 0:

                print(
                    f"[ARCHIVE] Получено кадров: "
                    f"{total}; {client_id}; "
                    f"{len(frame)} байт"
                )

    except WebSocketDisconnect:

        print(
            f"[ARCHIVE] WebSocket отключён: {client_id}"
        )

    except asyncio.TimeoutError:

        print(
            f"[ARCHIVE] Таймаут авторизации: {client_id}"
        )

    except Exception as e:

        print(
            f"[ARCHIVE] Ошибка WebSocket: "
            f"{type(e).__name__}: {e}"
        )


# ============================================================
# CLIENT LIST
# ============================================================

@app.get("/api/clients")
async def clients(request: Request):

    authorize_request(request)

    now = time.time()

    with CLIENT_LOCK:

        expired = [
            key
            for key, value in CLIENTS.items()
            if now - value["last_seen"] > CLIENT_TIMEOUT
        ]

        for key in expired:

            print(
                f"[ARCHIVE] Игрок удалён по таймауту: {key}"
            )

            del CLIENTS[key]

        result = [

            {
                "id": c["id"],
                "nickname": c["nickname"],
                "has_frame": c["frame"] is not None,
                "fps": round(c["fps"], 1)
            }

            for c in CLIENTS.values()
        ]

    return {
        "total": len(result),
        "clients": result
    }


# ============================================================
# PREVIEW IMAGE
# ============================================================

@app.get("/preview/{client_id}")
async def preview(
    client_id: str,
    request: Request
):

    authorize_request(request)

    with CLIENT_LOCK:

        client = CLIENTS.get(client_id)

        if client is None:
            raise HTTPException(
                404,
                "Minecraft user not found"
            )

        frame = client["frame"]

    if frame is None:
        raise HTTPException(
            404,
            "No frame available"
        )

    return Response(
        frame,
        media_type="image/webp",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate"
        }
    )


# ============================================================
# VIEW PLAYER
# ============================================================

@app.get("/view/{client_id}", response_class=HTMLResponse)
async def view_minecraft(client_id: str):

    with CLIENT_LOCK:

        client = CLIENTS.get(client_id)

        if client is None:
            raise HTTPException(
                404,
                "Minecraft user not found"
            )

        nickname = html.escape(
            client["nickname"]
        )

    safe_id = json.dumps(client_id)

    page = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>PLAYER_NAME — Archive</title>

<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #090909;
    color: #eee;
    font-family: Segoe UI, Arial, sans-serif;
}

header {
    height: 62px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 26px;
    border-bottom: 1px solid #292929;
    background: #101010;
}

.brand {
    font-weight: 700;
    letter-spacing: 2px;
}

a {
    color: #aaa;
    text-decoration: none;
}

a:hover {
    color: #fff;
}

main {
    max-width: 1500px;
    margin: 0 auto;
    padding: 24px;
}

.top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 18px;
}

h1 {
    font-size: 20px;
    font-weight: 600;
    margin: 0;
}

.status {
    font-size: 12px;
    color: #999;
}

.frame {
    width: 100%;
    height: calc(100vh - 150px);
    min-height: 300px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #030303;
    border: 1px solid #292929;
    border-radius: 5px;
    overflow: hidden;
}

img {
    display: block;
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}

.empty {
    color: #777;
    font-size: 13px;
}
</style>
</head>

<body>

<header>
    <div class="brand">ARCHIVE</div>
    <a href="/">← USERS</a>
</header>

<main>

    <div class="top">
        <h1>PLAYER_NAME</h1>
        <div class="status" id="status">CONNECTING</div>
    </div>

    <div class="frame">

        <img id="screen" alt="Minecraft">

        <div class="empty" id="empty">
            Ожидание изображения Minecraft…
        </div>

    </div>

</main>

<script>

const id = PLAYER_ID;

const img = document.getElementById('screen');
const empty = document.getElementById('empty');
const status = document.getElementById('status');

let password = sessionStorage.getItem('archive_password');

if (!password) {
    password = prompt('Пароль Archive:');
}

if (password) {
    sessionStorage.setItem('archive_password', password);
}

let updateInProgress = false;

async function update() {

    if (!password || updateInProgress) return;

    updateInProgress = true;

    try {

        const r = await fetch(
            '/preview/' + encodeURIComponent(id) + '?t=' + Date.now(),
            {
                cache: 'no-store',
                headers: {
                    'Authorization': 'Bearer ' + password
                }
            }
        );

        if (r.status === 401) {
            throw Error('Неверный пароль');
        }

        if (!r.ok) {
            throw Error('Ожидание изображения');
        }

        const blob = await r.blob();
        const url = URL.createObjectURL(blob);

        const old = img.dataset.url;

        img.onload = () => {
            if (old) URL.revokeObjectURL(old);
        };

        img.onerror = () => {
            URL.revokeObjectURL(url);
        };

        img.src = url;
        img.dataset.url = url;

        img.style.display = 'block';
        empty.style.display = 'none';

        status.textContent = 'ONLINE';

    } catch (e) {

        status.textContent = e.message;

    } finally {

        updateInProgress = false;

    }
}

img.style.display = 'none';

update();

setInterval(update, 1000 / 15);

</script>

</body>
</html>'''

    page = page.replace(
        "PLAYER_NAME",
        nickname
    )

    page = page.replace(
        "PLAYER_ID",
        safe_id
    )

    return HTMLResponse(page)


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard():

    return HTMLResponse(r'''<!doctype html>
<html lang="ru">

<head>

<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Archive</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #090909;
    color: #eee;
    font-family: Segoe UI, Arial, sans-serif;
}

header {
    height: 68px;
    background: #101010;
    border-bottom: 1px solid #292929;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 30px;
}

.brand {
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 3px;
}

.sub {
    font-size: 11px;
    color: #777;
    letter-spacing: 1px;
}

main {
    max-width: 1500px;
    margin: auto;
    padding: 30px;
}

.heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    margin-bottom: 22px;
}

h1 {
    font-size: 22px;
    font-weight: 600;
    margin: 0 0 5px;
}

.count {
    font-size: 12px;
    color: #888;
}

#search {
    width: 260px;
    background: #111;
    border: 1px solid #303030;
    color: #eee;
    padding: 11px 13px;
    border-radius: 4px;
    outline: none;
}

#grid {
    display: grid;
    grid-template-columns: repeat(
        auto-fill,
        minmax(260px, 1fr)
    );
    gap: 16px;
}

.card {
    background: #111;
    border: 1px solid #292929;
    border-radius: 5px;
    overflow: hidden;
    cursor: pointer;
    transition: border-color .15s, transform .15s;
}

.card:hover {
    border-color: #777;
    transform: translateY(-2px);
}

.preview,
.placeholder {
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #050505;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #666;
    object-fit: contain;
}

.info {
    padding: 14px 15px;
}

.nick {
    font-size: 14px;
    font-weight: 600;
}

.meta {
    display: flex;
    justify-content: space-between;
    color: #777;
    font-size: 11px;
    margin-top: 8px;
}

.dot {
    color: #aaa;
}

.empty {
    grid-column: 1 / -1;
    text-align: center;
    color: #777;
    padding: 60px 10px;
    border: 1px dashed #303030;
}

@media(max-width:600px) {

    main {
        padding: 16px;
    }

    .heading {
        align-items: stretch;
        gap: 14px;
        flex-direction: column;
    }

    #search {
        width: 100%;
    }

    header {
        padding: 0 16px;
    }

}

</style>

</head>

<body>

<header>

    <div class="brand">ARCHIVE</div>

    <div class="sub">MINECRAFT USERS</div>

</header>

<main>

    <div class="heading">

        <div>

            <h1>Players</h1>

            <div class="count" id="count">
                Подключение…
            </div>

        </div>

        <input
            id="search"
            placeholder="Поиск игрока…"
        >

    </div>

    <div id="grid">

        <div class="empty">
            Ожидание игроков Minecraft…
        </div>

    </div>

</main>

<script>

const grid = document.getElementById('grid');
const count = document.getElementById('count');
const search = document.getElementById('search');

let password = sessionStorage.getItem('archive_password');

if (!password) {
    password = prompt('Пароль Archive:');
}

if (password) {
    sessionStorage.setItem('archive_password', password);
}

let users = [];
let updateInProgress = false;

async function loadFrame(user, img) {

    try {

        const r = await fetch(
            '/preview/' + encodeURIComponent(user.id),
            {
                cache: 'no-store',
                headers: {
                    'Authorization': 'Bearer ' + password
                }
            }
        );

        if (!r.ok) return;

        const blob = await r.blob();
        const url = URL.createObjectURL(blob);

        const old = img.dataset.url;

        img.onload = () => {
            if (old) URL.revokeObjectURL(old);
        };

        img.onerror = () => {
            URL.revokeObjectURL(url);
        };

        img.src = url;
        img.dataset.url = url;

    } catch (e) {

        console.error('Frame error:', e);

    }

}

function render() {

    const q = search.value.trim().toLowerCase();

    const list = users.filter(
        u => u.nickname.toLowerCase().includes(q)
    );

    grid.replaceChildren();

    if (!list.length) {

        const e = document.createElement('div');

        e.className = 'empty';

        e.textContent = users.length
            ? 'Игроки не найдены'
            : 'Ожидание игроков Minecraft…';

        grid.append(e);

        return;
    }

    for (const user of list) {

        const card = document.createElement('div');

        card.className = 'card';

        card.onclick = () => {
            location.href = '/view/' + encodeURIComponent(user.id);
        };

        if (user.has_frame) {

            const im = document.createElement('img');

            im.className = 'preview';

            im.alt = 'Minecraft preview';

            card.append(im);

            loadFrame(user, im);

        } else {

            const p = document.createElement('div');

            p.className = 'placeholder';

            p.textContent = 'Ожидание Minecraft…';

            card.append(p);

        }

        const info = document.createElement('div');

        info.className = 'info';

        const nick = document.createElement('div');

        nick.className = 'nick';

        nick.textContent = user.nickname;

        info.append(nick);

        const meta = document.createElement('div');

        meta.className = 'meta';

        const online = document.createElement('span');

        online.textContent = '● ONLINE';

        online.className = 'dot';

        const fps = document.createElement('span');

        fps.textContent = user.fps + ' FPS';

        meta.append(online, fps);

        info.append(meta);

        card.append(info);

        grid.append(card);

    }

}

async function update() {

    if (!password || updateInProgress) return;

    updateInProgress = true;

    try {

        const r = await fetch('/api/clients', {

            cache: 'no-store',

            headers: {
                'Authorization': 'Bearer ' + password
            }

        });

        if (r.status === 401) {
            throw Error('Неверный пароль');
        }

        if (!r.ok) {
            throw Error('Ошибка соединения: ' + r.status);
        }

        const data = await r.json();

        users = Array.isArray(data.clients)
            ? data.clients
            : [];

        count.textContent = data.total + ' подключено';

        render();

    } catch (e) {

        count.textContent = e.message;

        console.error('Dashboard error:', e);

    } finally {

        updateInProgress = false;

    }

}

search.addEventListener('input', render);

update();

setInterval(update, 3000);

</script>

</body>
</html>''')


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print()
    print("================================")
    print("       ARCHIVE SERVER")
    print("================================")
    print()
    print(f"Local:   http://127.0.0.1:{PORT}")
    print(f"Network: http://localhost:{PORT}")
    print(f"Password: {PASSWORD}")
    print()
    print("Server starting...")
    print()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        workers=1,
        ws_max_size=MAX_FRAME_SIZE
    )

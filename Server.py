import asyncio
import hashlib
import hmac
import html
import json
import threading
import time
import uvicorn

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, Response


# ============================================================
# SETTINGS
# ============================================================

HOST = "0.0.0.0"
PORT = 5000

PASSWORD = "1986"

CLIENT_TIMEOUT = 30
MAX_FRAME_SIZE = 32 * 1024 * 1024

CLIENTS = {}
CLIENT_LOCK = threading.Lock()


# ============================================================
# AUTHENTICATION
# ============================================================

def check_password(password):
    if not isinstance(password, str):
        return False

    return hmac.compare_digest(
        password.encode("utf-8"),
        PASSWORD.encode("utf-8")
    )


def authorize_request(request: Request):
    authorization = request.headers.get("Authorization", "")

    scheme, separator, password = authorization.partition(" ")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not check_password(password)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid password"
        )


# ============================================================
# APPLICATION
# ============================================================

@asynccontextmanager
async def lifespan(app):
    print("[SERVER] Starting...")
    print("[SERVER] Password protection enabled.")
    yield
    print("[SERVER] Stopped.")


app = FastAPI(lifespan=lifespan)


# ============================================================
# CLIENT MANAGEMENT
# ============================================================

def remove_expired_clients():
    now = time.time()

    with CLIENT_LOCK:
        expired = [
            client_id
            for client_id, client in CLIENTS.items()
            if now - client["last_seen"] > CLIENT_TIMEOUT
        ]

        for client_id in expired:
            print(
                "[TIMEOUT]",
                CLIENTS[client_id]["name"]
            )

            del CLIENTS[client_id]


@app.post("/api/register")
async def register(request: Request):
    authorize_request(request)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    client_id = str(data.get("client_id", "")).strip()
    name = str(data.get("name", "")).strip()[:80]

    if not client_id or len(client_id) > 64:
        raise HTTPException(400, "Invalid client ID")

    name = name or "Unnamed PC"

    now = time.time()

    with CLIENT_LOCK:
        if client_id not in CLIENTS:
            CLIENTS[client_id] = {
                "id": client_id,
                "name": name,
                "frame": None,
                "last_seen": now,
                "fps": 0.0,
                "frame_count": 0,
                "fps_start": now
            }

            print("[REGISTERED]", name)

        else:
            CLIENTS[client_id]["name"] = name
            CLIENTS[client_id]["last_seen"] = now

    return {"status": "ok"}


# ============================================================
# WEBSOCKET STREAM
# ============================================================

@app.websocket("/ws/{client_id}")
async def screen_stream(
    websocket: WebSocket,
    client_id: str
):
    await websocket.accept()

    # First WebSocket message must be the password.
    try:
        password = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=10
        )

    except Exception:
        await websocket.close(
            code=1008,
            reason="Password required"
        )
        return

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
                reason="Client not registered"
            )
            return

        CLIENTS[client_id]["last_seen"] = time.time()

    await websocket.send_text("AUTH_OK")

    print("[STREAM CONNECTED]", client_id)

    try:
        while True:
            frame = await websocket.receive_bytes()

            if not frame:
                continue

            if len(frame) > MAX_FRAME_SIZE:
                print("[WARNING] Frame too large:", len(frame))
                continue

            # Check WebP signature: RIFF....WEBP
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

                # Keep only the newest frame in RAM.
                client["frame"] = frame
                client["last_seen"] = now

                client["frame_count"] += 1

                elapsed = now - client["fps_start"]

                if elapsed >= 1:
                    client["fps"] = (
                        client["frame_count"] / elapsed
                    )

                    client["frame_count"] = 0
                    client["fps_start"] = now

    except WebSocketDisconnect:
        pass

    except Exception as error:
        print("[STREAM ERROR]", error)

    finally:
        print("[STREAM DISCONNECTED]", client_id)


# ============================================================
# CLIENT LIST
# ============================================================

@app.get("/api/clients")
async def get_clients(request: Request):
    authorize_request(request)

    remove_expired_clients()

    with CLIENT_LOCK:
        result = []

        for client in CLIENTS.values():
            result.append({
                "id": client["id"],
                "name": client["name"],
                "has_frame": client["frame"] is not None,
                "fps": round(client["fps"], 1)
            })

    return {
        "total": len(result),
        "clients": result
    }


# ============================================================
# FRAME PREVIEW
# ============================================================

@app.get("/preview/{client_id}")
async def preview(
    client_id: str,
    request: Request
):
    authorize_request(request)

    with CLIENT_LOCK:
        client = CLIENTS.get(client_id)

        if client is None or client["frame"] is None:
            raise HTTPException(
                404,
                "No frame available"
            )

        frame = client["frame"]

    return Response(
        content=frame,
        media_type="image/webp",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache"
        }
    )


# ============================================================
# VIEW PAGE
# ============================================================

@app.get("/view/{client_id}")
async def view_pc(client_id: str):
    with CLIENT_LOCK:
        client = CLIENTS.get(client_id)

        if client is None:
            raise HTTPException(404, "PC not found")

        name = html.escape(client["name"])

    safe_id = json.dumps(client_id)

    return HTMLResponse(f"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>{name} - PC Network</title>

<style>
* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    background: #101216;
    color: #eee;
    font-family: Arial, sans-serif;
}}

header {{
    height: 58px;
    background: #191d24;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 18px;
}}

a {{
    color: #8ab4ff;
    text-decoration: none;
}}

#screen {{
    display: block;
    width: 100%;
    height: calc(100vh - 58px);
    object-fit: contain;
    background: #08090b;
}}

#status {{
    color: #70db9a;
    font-size: 13px;
}}
</style>
</head>

<body>

<header>
    <div>
        <a href="/">← Компьютеры</a>
        &nbsp; | &nbsp;
        <b>{name}</b>
    </div>

    <div id="status">Ожидание...</div>
</header>

<img id="screen" alt="Экран компьютера">

<script>
const id = {safe_id};

const img = document.getElementById("screen");
const status = document.getElementById("status");

let password = sessionStorage.getItem("pcnet_password");

if (!password) {{
    password = prompt("Введите пароль:");
}}

if (password) {{
    sessionStorage.setItem("pcnet_password", password);
}}

let previousUrl = null;
let busy = false;

async function updateFrame() {{
    if (busy || !password) return;

    busy = true;

    try {{
        const response = await fetch(
            "/preview/" + encodeURIComponent(id),
            {{
                cache: "no-store",
                headers: {{
                    "Authorization": "Bearer " + password
                }}
            }}
        );

        if (response.status === 401) {{
            sessionStorage.removeItem("pcnet_password");
            password = "";
            status.textContent = "Неверный пароль";
            return;
        }}

        if (!response.ok) {{
            throw new Error("Ожидание кадра...");
        }}

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);

        img.onload = () => {{
            if (previousUrl) {{
                URL.revokeObjectURL(previousUrl);
            }}

            previousUrl = url;
        }};

        img.src = url;

        status.textContent = "Трансляция активна";

    }} catch (error) {{
        status.textContent = error.message;
    }} finally {{
        busy = false;
    }}
}}

setInterval(updateFrame, 1000 / 30);
updateFrame();
</script>

</body>
</html>
""")


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>PC Network</title>

<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #101216;
    color: #e9edf3;
    font-family: Arial, sans-serif;
}

header {
    background: #191d24;
    padding: 20px 26px;
    border-bottom: 1px solid #303640;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

h1 {
    font-size: 21px;
    margin: 0;
}

main {
    padding: 22px;
    max-width: 1700px;
    margin: auto;
}

input {
    width: 100%;
    padding: 12px;
    background: #202630;
    color: white;
    border: 1px solid #343d49;
    border-radius: 8px;
    margin-bottom: 20px;
    font-size: 15px;
}

#status {
    color: #9ca8b8;
}

#grid {
    display: grid;
    grid-template-columns: repeat(
        auto-fill,
        minmax(250px, 1fr)
    );
    gap: 17px;
}

.card {
    background: #1a1f27;
    border: 1px solid #303844;
    border-radius: 11px;
    overflow: hidden;
    cursor: pointer;
}

.card:hover {
    border-color: #668fe0;
}

.preview,
.placeholder {
    width: 100%;
    aspect-ratio: 16 / 9;
    object-fit: contain;
    background: #080a0e;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #788396;
}

.info {
    padding: 13px;
}

.name {
    font-weight: bold;
    overflow-wrap: anywhere;
}

.meta {
    color: #70d99a;
    margin-top: 7px;
    font-size: 12px;
}

.empty {
    color: #8994a5;
    padding: 35px;
    text-align: center;
    grid-column: 1 / -1;
}
</style>
</head>

<body>

<header>
    <h1>PC Network</h1>
    <div id="status">Подключение...</div>
</header>

<main>

<input
    id="search"
    placeholder="Поиск компьютера..."
>

<div id="grid">
    <div class="empty">Ожидание компьютеров...</div>
</div>

</main>

<script>
const grid = document.getElementById("grid");
const status = document.getElementById("status");
const search = document.getElementById("search");

let password = sessionStorage.getItem("pcnet_password");

if (!password) {
    password = prompt("Введите пароль PC Network:");
}

if (password) {
    sessionStorage.setItem("pcnet_password", password);
}

let clients = [];

async function update() {
    if (!password) return;

    try {
        const response = await fetch("/api/clients", {
            cache: "no-store",
            headers: {
                "Authorization": "Bearer " + password
            }
        });

        if (response.status === 401) {
            sessionStorage.removeItem("pcnet_password");
            password = "";
            status.textContent = "Неверный пароль";
            return;
        }

        if (!response.ok) {
            throw new Error();
        }

        const data = await response.json();

        clients = data.clients;

        status.textContent =
            "Подключено компьютеров: " + data.total;

        render();

    } catch {
        status.textContent = "Ошибка соединения";
    }
}

async function loadPreview(client, image) {
    try {
        const response = await fetch(
            "/preview/" + encodeURIComponent(client.id),
            {
                cache: "no-store",
                headers: {
                    "Authorization": "Bearer " + password
                }
            }
        );

        if (!response.ok) return;

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);

        const oldUrl = image.dataset.objectUrl;

        image.src = url;
        image.dataset.objectUrl = url;

        if (oldUrl) {
            URL.revokeObjectURL(oldUrl);
        }

    } catch {
        // Ignore temporary preview errors.
    }
}

function render() {
    const query = search.value.trim().toLowerCase();

    const filtered = clients.filter(client =>
        client.name.toLowerCase().includes(query)
    );

    grid.replaceChildren();

    if (!filtered.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Нет подключённых компьютеров";
        grid.appendChild(empty);
        return;
    }

    for (const client of filtered) {
        const card = document.createElement("div");
        card.className = "card";

        card.onclick = () => {
            location.href =
                "/view/" + encodeURIComponent(client.id);
        };

        if (client.has_frame) {
            const img = document.createElement("img");

            img.className = "preview";
            img.alt = "Предпросмотр";

            card.appendChild(img);
            loadPreview(client, img);

        } else {
            const placeholder = document.createElement("div");

            placeholder.className = "placeholder";
            placeholder.textContent = "Ожидание изображения...";

            card.appendChild(placeholder);
        }

        const info = document.createElement("div");
        info.className = "info";

        const name = document.createElement("div");
        name.className = "name";
        name.textContent = client.name;

        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent =
            "● Подключён | " + client.fps + " FPS";

        info.append(name, meta);
        card.appendChild(info);

        grid.appendChild(card);
    }
}

search.addEventListener("input", render);

update();
setInterval(update, 3000);
</script>

</body>
</html>
""")


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("=" * 55)
    print("                 PC NETWORK SERVER")
    print("=" * 55)
    print("Local URL: http://127.0.0.1:5000")
    print("Password:", PASSWORD)
    print("Frame storage: RAM (latest frame only)")
    print("Press CTRL+C to stop.")
    print()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        workers=1,
        ws_max_size=MAX_FRAME_SIZE
    )
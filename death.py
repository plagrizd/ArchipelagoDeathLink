#!/usr/bin/env python3
# ArchiDeathLink (public-clean)
# - Keeps /stats HTML overlay (no separate stats.html file)
# - No Twitch API, no .env
# - Optional auth via config.json["auth_key"] (X-Auth-Key header or ?auth_key=)
# - HTTP binds to 127.0.0.1 (local only) by default; change if you need LAN

import asyncio
import websockets
import datetime
import json
import os
import random
import logging
import ssl
from aiohttp import web
import time
import uuid
from collections import deque
from pathlib import Path

# ----------------- Files & Globals -----------------

APP_DIR = Path(__file__).parent.resolve()

CONFIG_FILE = str(APP_DIR / "config.json")
DEATHLINK_QUEUE_FILE = str(APP_DIR / "deathlink_queue.txt")
STATS_FILE = str(APP_DIR / "stats.txt")
TRIGGER_FILE = str(APP_DIR / "deathlink_trigger.txt")
DL_IMAGE_FILE = str(APP_DIR / "DL.png")

deathlink_stats = {}                 # contributors (outbound triggers)
player_death_stats = {}              # inbound non-bot player deaths
recent_outbound = deque(maxlen=100)  # (nonce, source, ts)

client_connected = False
archipelago_ws = None

# ----------------- Config -----------------

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("⚠️ Config file not found! A new one will be created with defaults.")
        return {
            "deaths_per_twitch_sub": 3,
            "deaths_per_tiktok_sub": 2,
            "bits_per_death": 100,
            "coins_per_death": 100,
            "http_port": 5000,
            "websocket_port": "54802",
            "min_dispatch_seconds": 240,
            "max_dispatch_seconds": 480,
            # Optional: set to "" or remove to disable auth entirely
            "auth_key": ""
        }
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

config = load_config()

# Persist archipelago port entry
archipelago_port = input(f"Enter Archipelago server port [{config.get('websocket_port', 'NONE')}]: ").strip()
if not archipelago_port:
    archipelago_port = config.get("websocket_port", None)

if not archipelago_port:
    print("❌ Missing required values! Please enter the Archipelago port.")
    raise SystemExit(1)

config["websocket_port"] = archipelago_port
save_config(config)

HTTP_PORT = int(config.get("http_port", 5000))
AUTH_KEY = (config or {}).get("auth_key") or ""  # empty string disables auth

# ----------------- Logging (quiet) -----------------
logging.basicConfig(level=logging.WARNING)  # Show only warnings+errors
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("aiohttp.server").setLevel(logging.WARNING)
logging.getLogger("aiohttp.web").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
log = logging.getLogger("archi-deathlink")

# ----------------- Auth Helper -----------------

def authorized(request: web.Request) -> bool:
    if not AUTH_KEY:
        return True  # auth disabled
    return (
        request.headers.get("X-Auth-Key") == AUTH_KEY
        or request.query.get("auth_key") == AUTH_KEY
    )

def require_auth(request: web.Request):
    if not authorized(request):
        raise web.HTTPUnauthorized(text="Unauthorized")

# ----------------- Queue Helpers -----------------

def enqueue_deathlinks(name, count):
    queue = []
    if os.path.exists(DEATHLINK_QUEUE_FILE):
        with open(DEATHLINK_QUEUE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            queue = [line.strip() for line in f if line.strip()]

    queue.extend([name] * max(0, int(count)))

    # Write as UTF-8 so emoji etc. don't blow up on Windows
    with open(DEATHLINK_QUEUE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(queue))

    return len(queue)

def dequeue_deathlink():
    queue = []
    if os.path.exists(DEATHLINK_QUEUE_FILE):
        with open(DEATHLINK_QUEUE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            queue = [line.strip() for line in f if line.strip()]
    if not queue:
        return None, 0
    name = queue.pop(0)
    with open(DEATHLINK_QUEUE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(queue))
    return name, len(queue)

# ----------------- WS Relay -----------------

async def relay_messages(client_ws, path):
    global archipelago_ws, client_connected
    ARCHIPELAGO_SERVER = f"wss://archipelago.gg:{archipelago_port}"
    print(f"Connecting to Archipelago at {ARCHIPELAGO_SERVER}")
    try:
        # Prefer verified context; fall back to unverified if necessary
        try:
            ssl_context = ssl.create_default_context()
        except Exception:
            ssl_context = ssl._create_unverified_context()

        async with websockets.connect(ARCHIPELAGO_SERVER, ssl=ssl_context) as server_ws:
            archipelago_ws = server_ws
            client_connected = True

            async def client_to_server():
                async for message in client_ws:
                    # Forward to Archipelago
                    await server_ws.send(message)

            async def _clear_banner_soon():
                await asyncio.sleep(4)
                try:
                    with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
                        f.write("")
                except Exception:
                    pass

            async def server_to_client():
                async for message in server_ws:
                    # Always forward to our local Archipelago client
                    await client_ws.send(message)

                    # Also parse for inbound (bounced) player DeathLinks
                    try:
                        data = json.loads(message)
                        if isinstance(data, list):
                            for item in data:
                                if (isinstance(item, dict)
                                    and item.get("cmd") in ("Bounced", "Bounce")
                                    and "DeathLink" in item.get("tags", [])
                                    and isinstance(item.get("data"), dict)):
                                    dl = item["data"]
                                    origin = dl.get("origin")
                                    nonce = dl.get("nonce")
                                    source = dl.get("source", "UNKNOWN")

                                    # Ignore our own outbound (we tagged them)
                                    is_our_origin = (origin == "LOCAL_BOT")
                                    is_our_nonce = any(nonce == n for (n, _, _) in recent_outbound)

                                    if is_our_origin or is_our_nonce:
                                        continue

                                    # Count as a player death
                                    player_death_stats[source] = player_death_stats.get(source, 0) + 1

                                    # Trigger overlay for inbound player deaths
                                    with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
                                        f.write(source)
                                    asyncio.create_task(_clear_banner_soon())
                    except Exception:
                        # swallow parsing errors; keep relay alive
                        pass

            await asyncio.gather(client_to_server(), server_to_client())

    except Exception as e:
        logging.error(f"WebSocket Relay Error: {e}")
    finally:
        print("🔌 Client disconnected. Resetting WebSocket state...")
        client_connected = False
        archipelago_ws = None

# ----------------- DeathLink send -----------------

async def send_deathlink(source_name="CHAT"):
    if not archipelago_ws or not client_connected:
        print("⚠️ Client not connected. Cannot send DeathLink.")
        return

    # Track contributor totals
    deathlink_stats[source_name] = deathlink_stats.get(source_name, 0) + 1

    # Tag outbound so we can ignore on bounce
    nonce = str(uuid.uuid4())
    deathlink_payload = {
        "cmd": "Bounce",
        "tags": ["DeathLink"],
        "data": {
            "time": datetime.datetime.now().timestamp(),
            "source": source_name,
            "cause": f"Killed by {source_name}",
            "origin": "LOCAL_BOT",
            "nonce": nonce
        }
    }

    try:
        await archipelago_ws.send(json.dumps([deathlink_payload]))
        recent_outbound.append((nonce, source_name, time.time()))

        print(f"💀 DeathLink sent! (source: {source_name})")
        print(f"📊 {source_name} has caused {deathlink_stats[source_name]} DeathLink(s) this session.")

        # Update stats file
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            for user, count in sorted(deathlink_stats.items(), key=lambda x: -x[1]):
                f.write(f"{user}: {count}\n")

        # Trigger overlay banner
        with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
            f.write(source_name)
        await asyncio.sleep(4)
        with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
            f.write("")

    except Exception as e:
        print(f"⚠️ Error sending DeathLink: {e}")

# ----------------- Dispatcher -----------------

async def staged_deathlink_dispatcher():
    while True:
        name, remaining = dequeue_deathlink()
        if name:
            if not client_connected:
                print(f"⛔ Skipping DeathLink for {name} — no client connected.")
                await asyncio.sleep(5)
                continue

            print(f"🧮 {remaining + 1} DeathLinks remaining... triggering one now.")
            await send_deathlink(name)

            # Configurable delay
            min_s = int(config.get("min_dispatch_seconds", 240))
            max_s = int(config.get("max_dispatch_seconds", 480))
            if max_s < min_s:
                max_s = min_s
            await asyncio.sleep(random.randint(min_s, max_s))
        else:
            await asyncio.sleep(5)

# ----------------- HTTP Routes -----------------

routes = web.RouteTableDef()

@routes.get("/DL.png")
async def handle_dl_image(request):
    if os.path.exists(DL_IMAGE_FILE):
        return web.FileResponse(DL_IMAGE_FILE)
    return web.Response(status=404, text="DL.png not found")

@routes.get('/deathlink_trigger.txt')
async def handle_deathlink_trigger(request):
    if os.path.exists(TRIGGER_FILE):
        return web.FileResponse(TRIGGER_FILE)
    else:
        return web.Response(status=404, text="Not Found")

# ensure trigger file exists at boot
with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
    f.write("")

@routes.get("/stats")
async def handle_stats_page(request):
    # NOTE: This returns a complete HTML overlay page (no external stats.html needed)
    view = request.query_string

    # Top killer + totals
    top_user, top_count = ("None", 0)
    total = sum(deathlink_stats.values())
    if deathlink_stats:
        top_user, top_count = max(deathlink_stats.items(), key=lambda x: x[1])

    # Queue length
    queue = []
    if os.path.exists(DEATHLINK_QUEUE_FILE):
        with open(DEATHLINK_QUEUE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            queue = [line.strip() for line in f if line.strip()]
    queue_length = len(queue)

    # Contributors (exclude top for variety)
    contributors = [f"{k} ({v})" for k, v in deathlink_stats.items() if k != top_user]
    random.shuffle(contributors)
    contrib_json = json.dumps(contributors)

    # Player deaths (from inbound, non-bot)
    players = [f"{k} ({v})" for k, v in player_death_stats.items()]
    random.shuffle(players)
    players_json = json.dumps(players)

    # Page bodies
    if "rates" in view:
        content = f"""
        <h1>DeathLink Trigger Rates</h1>
        <ul>
            <li>🎯 {config.get("bits_per_death", 100)} Bits = 1 Death</li>
            <li>🪙 {config.get("coins_per_death", 100)} Coins = 1 Death</li>
            <li>💜 1 Twitch Sub = {config.get("deaths_per_twitch_sub", 3)} Deaths</li>
            <li>🎵 1 TikTok Sub = {config.get("deaths_per_tiktok_sub", 2)} Deaths</li>
        </ul>
        """
    else:
        # either ?deaths or full view
        base_stats = f"""
        <h1>DeathLink Stats</h1>
        <p>Top Killer: <strong>{top_user}</strong> ({top_count})</p>
        <p>Deaths in Queue: <strong>{queue_length}</strong></p>
        <p>Deaths This Run: <strong>{total}</strong></p>

        <p><strong>Contributors:</strong></p>
        <div class="contributor-box"><p id="contributorText">Loading...</p></div>

        <p style="margin-top:18px;"><strong>Player Deaths:</strong></p>
        <div class="player-box"><p id="playerText">Loading...</p></div>
        """

        if "deaths" in view:
            content = base_stats
        else:
            # full stats + rates
            content = base_stats + f"""
            <div class="rates">
                <h2>Trigger Rates</h2>
                <ul>
                    <li>🎯 <strong>{config.get("bits_per_death", 100)}</strong> Bits = 1 Death</li>
                    <li>🪙 <strong>{config.get("coins_per_death", 100)}</strong> Coins = 1 Death</li>
                    <li>💜 1 Twitch Sub = <strong>{config.get("deaths_per_twitch_sub", 3)}</strong> Deaths</li>
                    <li>🎵 1 TikTok Sub = <strong>{config.get("deaths_per_tiktok_sub", 2)}</strong> Deaths</li>
                </ul>
            </div>
            """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>DeathLink Stats</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
            html, body {{ margin:0; padding:0; height:100%; width:100%; background-color:#111; font-family: monospace; color:#eee; }}
            body::before {{ content:""; background:url('/DL.png') no-repeat center center; background-size:contain; opacity:0.08; position:fixed; top:0; left:0; width:100%; height:100%; z-index:0; }}
            .container {{ display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100vh; text-align:center; z-index:1; padding:24px; box-sizing:border-box; }}
            h1 {{ font-size:48px; margin-bottom:20px; }}
            p {{ font-size:24px; margin:10px 0; }}
            .rates {{ margin-top:30px; background-color:transparent; padding:20px; border-radius:8px; text-align:left; }} /* transparent background */
            .rates h2 {{ font-size:28px; margin-bottom:10px; text-align:center; }}
            .rates ul {{ list-style:none; padding-left:0; }}
            .rates li {{ font-size:18px; margin:4px 0; }}
            .contributor-box, .player-box {{ min-height:28px; transition: all 0.3s ease; }}
            #contributorText, #playerText {{ transition: opacity 0.5s ease-in-out; }}
            #deathBanner {{ position:fixed; left:50%; transform: translate(-50%, -50%) rotate(-30deg); font-size:60px; color:red; opacity:0; pointer-events:none; z-index:9999; font-weight:bold; transition: opacity 1s ease-in-out; }}
            .minor {{ font-size:14px; opacity:.7; margin-top:16px; }}
        </style>
    </head>
    <body>
        <div class="container">
            {content}
            {"<div class='minor'>Auth is enabled. Append <code>?auth_key=***</code> to protected endpoints.</div>" if AUTH_KEY else ""}
        </div>
        <div id="deathBanner"></div>
        <script>
            const contributors = {json.dumps(contrib_json)};
            const players = {json.dumps(players_json)};
            let ci = 0, pi = 0;

            function rotateText(list, elId) {{
                const el = document.getElementById(elId);
                if (!el || !list || list.length === 0) return;
                el.style.opacity = 0;
                setTimeout(() => {{
                    const idx = (elId === "contributorText") ? (ci = (ci + 1) % list.length) : (pi = (pi + 1) % list.length);
                    el.textContent = list[idx];
                    el.style.opacity = 1;
                }}, 500);
            }}

            // Kick off rotations (every 3s)
            setInterval(() => rotateText(contributors, "contributorText"), 3000);
            setInterval(() => rotateText(players, "playerText"), 3000);
            // First draw immediately
            if (contributors.length) document.getElementById("contributorText").textContent = contributors[0];
            if (players.length) document.getElementById("playerText").textContent = players[0];

            // Adjust banner vertical position for ?deaths path
            const bannerEl = document.getElementById("deathBanner");
            if (window.location.search.includes("deaths")) {{
                bannerEl.style.top = "50%";
            }} else {{
                bannerEl.style.top = "40%";
            }}

            // DeathLink animation (poll the trigger file)
            let lastUser = "";
            async function checkDeathTrigger() {{
                try {{
                    const resp = await fetch('/deathlink_trigger.txt', {{ cache: 'no-store' }});
                    if (!resp.ok) return;
                    const text = await resp.text();
                    if (text && text !== lastUser) {{
                        lastUser = text;
                        bannerEl.textContent = text;
                        bannerEl.style.opacity = 1;
                        setTimeout(() => {{ bannerEl.style.opacity = 0; }}, 3000);
                    }}
                }} catch (e) {{}}
            }}
            setInterval(checkDeathTrigger, 1000);

            // Light refresh to keep numbers current
            setInterval(() => location.reload(), 10000);
        </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

# ---- Endpoints that trigger 1 immediately, queue remainder ----

@routes.get("/twitch")
async def handle_twitch_sub(request):
    require_auth(request)
    user = request.query.get("user", "Twitch")
    qty = int(request.query.get("qty", 1))
    per = int(config.get("deaths_per_twitch_sub", 3))
    total_deaths = qty * per
    if total_deaths <= 0:
        return web.Response(text="No deaths to process.", status=200)

    # one immediate, rest queued
    await send_deathlink(user)
    if total_deaths > 1:
        enqueue_deathlinks(user, total_deaths - 1)
    return web.Response(text=f"Triggered 1 & queued {max(0, total_deaths - 1)} deaths for {user} (Twitch subs).", status=200)

@routes.get("/tiktok")
async def handle_tiktok_sub(request):
    require_auth(request)
    user = request.query.get("user", "TikTok")
    qty = int(request.query.get("qty", 1))  # allow qty for convenience
    per = int(config.get("deaths_per_tiktok_sub", 2))
    total_deaths = qty * per
    if total_deaths <= 0:
        return web.Response(text="No deaths to process.", status=200)

    await send_deathlink(user)
    if total_deaths > 1:
        enqueue_deathlinks(user, total_deaths - 1)
    return web.Response(text=f"Triggered 1 & queued {max(0, total_deaths - 1)} deaths for {user} (TikTok subs).", status=200)

@routes.get("/custom")
async def handle_custom(request):
    require_auth(request)
    user = request.query.get("user", "Custom")
    qty = int(request.query.get("qty", 1))
    if qty <= 0:
        return web.Response(text="No deaths to process.", status=200)

    await send_deathlink(user)
    if qty > 1:
        enqueue_deathlinks(user, qty - 1)
    return web.Response(text=f"Triggered 1 & queued {max(0, qty - 1)} custom deaths for {user}.", status=200)

# ---- Bits / Coins (immediate + queued remainder) ----

@routes.get("/cheer")
async def handle_cheer(request):
    require_auth(request)
    user = request.query.get("user", "Cheer")
    bits = int(request.query.get("qty", 0))
    per = int(config.get("bits_per_death", 100))
    num_deaths = bits // per
    if num_deaths > 0:
        await send_deathlink(user)
        if num_deaths > 1:
            enqueue_deathlinks(user, num_deaths - 1)
        return web.Response(text=f"{user} triggered 1 DeathLink and queued {num_deaths - 1}", status=200)
    return web.Response(text="Not enough bits for a DeathLink", status=200)

@routes.get("/coins")
async def handle_coins(request):
    require_auth(request)
    user = request.query.get("user", "Coins")
    coins = int(request.query.get("qty", 0))
    per = int(config.get("coins_per_death", 100))
    num_deaths = coins // per
    if num_deaths > 0:
        await send_deathlink(user)
        if num_deaths > 1:
            enqueue_deathlinks(user, num_deaths - 1)
        return web.Response(text=f"{user} triggered 1 DeathLink and queued {num_deaths - 1}", status=200)
    return web.Response(text="Not enough coins for a DeathLink", status=200)

@routes.get("/manual")
async def handle_manual(request):
    # Manual page stays unprotected for convenience; submit includes ?auth_key if set
    auth_hint = f"&auth_key={AUTH_KEY}" if AUTH_KEY else ""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>DeathLink Manual Trigger</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
            :root {{ color-scheme: dark; }}
            html, body {{
                margin: 0; padding: 0; height: 100%; width: 100%;
                background: #111; color: #eee; font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, 'Helvetica Neue', Arial, 'Noto Sans';
            }}
            .wrap {{
                max-width: 800px; margin: 40px auto; padding: 24px;
                background: #191919; border: 1px solid #2a2a2a; border-radius: 12px;
                box-shadow: 0 10px 24px rgba(0,0,0,.35);
            }}
            h1 {{ margin: 0 0 8px; font-size: 28px; }}
            p.desc {{ margin: 0 0 24px; color: #aaa; }}
            form {{ display: grid; gap: 16px; grid-template-columns: 1fr 1fr; align-items: end; }}
            label {{ display: block; font-size: 12px; color: #9a9a9a; margin-bottom: 6px; }}
            select, input[type="text"], input[type="number"], input[type="password"] {{
                width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #333; background: #121212; color: #eee;
                outline: none;
            }}
            input[type="number"] {{ -moz-appearance: textfield; }}
            input::-webkit-outer-spin-button, input::-webkit-inner-spin-button {{ -webkit-appearance: none; margin: 0; }}
            .row {{ display: contents; }}
            .actions {{ grid-column: 1 / -1; display: flex; gap: 10px; flex-wrap: wrap; }}
            button {{ padding: 10px 14px; background: #2b5cff; color: white; border: 0; border-radius: 8px; cursor: pointer; font-weight: 600; }}
            button.secondary {{ background: #333; }}
            .result {{
                margin-top: 16px; padding: 12px; border-radius: 8px; background: #151515; border: 1px solid #2a2a2a; min-height: 42px;
                font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
                white-space: pre-wrap;
            }}
            .pill {{ background: #222; border: 1px solid #333; padding: 6px 10px; border-radius: 999px; font-size: 12px; color: #bbb; }}
            .url-preview {{ color: #9fd1ff; word-break: break-all; }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <h1>DeathLink Manual Trigger</h1>
            <p class="desc">Fire any endpoint with a Username and QTY. This builds a link like <span class="pill">/<span id="ep-pill">twitch</span>?user=<span id="user-pill">User</span>&qty=<span id="qty-pill">1</span>{auth_hint}</span></p>

            <form id="manualForm">
                <div class="row">
                    <div>
                        <label>Endpoint</label>
                        <select id="endpoint">
                            <option value="twitch">/twitch</option>
                            <option value="tiktok">/tiktok</option>
                            <option value="cheer">/cheer</option>
                            <option value="coins">/coins</option>
                            <option value="custom">/custom</option>
                        </select>
                    </div>

                    <div>
                        <label>Username</label>
                        <input id="username" type="text" placeholder="Name (required)" required />
                    </div>
                </div>

                <div class="row">
                    <div>
                        <label>QTY</label>
                        <input id="qty" type="number" inputmode="numeric" min="0" step="1" value="1" />
                    </div>
                    <div>
                        <label>Auth Key (optional)</label>
                        <input id="auth" type="password" placeholder="auth_key if enabled" value="{AUTH_KEY}" />
                    </div>
                </div>

                <div class="actions">
                    <button type="submit">Submit</button>
                    <button type="button" class="secondary" id="openNew">Open in New Tab</button>
                </div>
            </form>

            <div id="preview" class="url-preview" style="margin-top:8px;"></div>
            <div id="result" class="result"></div>
        </div>

        <script>
            const epSel = document.getElementById('endpoint');
            const userInp = document.getElementById('username');
            const qtyInp = document.getElementById('qty');
            const authInp = document.getElementById('auth');
            const form = document.getElementById('manualForm');
            const result = document.getElementById('result');
            const preview = document.getElementById('preview');
            const epPill = document.getElementById('ep-pill');
            const userPill = document.getElementById('user-pill');
            const qtyPill = document.getElementById('qty-pill');
            const openNew = document.getElementById('openNew');

            function buildURL() {{
                const ep = epSel.value.trim();
                const user = (userInp.value || '').trim();
                const qty = Math.max(0, parseInt(qtyInp.value || '0', 10) || 0);
                const auth = (authInp.value || '').trim();
                const qsUser = encodeURIComponent(user || (ep === 'tiktok' ? 'TikTok' : ep === 'twitch' ? 'Twitch' : ep === 'cheer' ? 'Cheer' : ep === 'coins' ? 'Coins' : 'Custom'));
                const authPart = auth ? `&auth_key=${{encodeURIComponent(auth)}}` : '';
                const url = `/${{ep}}?user=${{qsUser}}&qty=${{qty}}${{authPart}}`;
                // live preview
                epPill.textContent = ep;
                userPill.textContent = user || '(default)';
                qtyPill.textContent = qty;
                preview.textContent = url;
                return url;
            }}

            // init preview
            buildURL();
            [epSel, userInp, qtyInp, authInp].forEach(el => el.addEventListener('input', buildURL));

            form.addEventListener('submit', async (e) => {{
                e.preventDefault();
                result.textContent = 'Working...';
                const url = buildURL();
                try {{
                    const resp = await fetch(url, {{ method: 'GET', cache: 'no-store' }});
                    const text = await resp.text();
                    result.textContent = text || `(HTTP ${{resp.status}})`;
                }} catch (err) {{
                    result.textContent = `Error: ${{err}}`;
                }}
            }});

            openNew.addEventListener('click', () => {{
                const url = buildURL();
                window.open(url, '_blank');
            }});
        </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

# ----------------- HTTP server -----------------

async def start_http_server():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    # Bind locally for safety; change to "0.0.0.0" if you really need external access
    site = web.TCPSite(runner, "127.0.0.1", HTTP_PORT)
    await site.start()
    print(f"🌐 HTTP server running at http://127.0.0.1:{HTTP_PORT}")

# ----------------- Start all -----------------

async def start_services():
    dispatcher_task = asyncio.create_task(staged_deathlink_dispatcher())
    relay_task = asyncio.create_task(start_websocket_relay())
    http_task = asyncio.create_task(start_http_server())
    await asyncio.gather(dispatcher_task, relay_task, http_task)

async def start_websocket_relay():
    print("🚀 Starting WebSocket relay on ws://localhost:42069")
    async with websockets.serve(relay_messages, "localhost", 42069):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(start_services())

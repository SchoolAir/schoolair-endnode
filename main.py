"""main.py

Entrypoint. Validates registration then starts:
  - Ingest job (reads sensors, checks alerts & posts measurements)
  - Microdot   (local web server: real-time dashboard + WebSocket)

Signal handling
  SIGTERM  — flush in-memory buffers to SQLite before exit (covers
             systemctl stop/restart, sudo reboot/poweroff)
  SIGUSR2  — trigger an immediate out-of-schedule drain
             (systemctl kill -s SIGUSR2 schoolair  or  kill -USR2 <pid>)
"""

import asyncio
import json
import os
import signal
import socket
from datetime import datetime, timezone
from pathlib import Path

import db.queue as queue
from dotenv import load_dotenv
from microdot import Microdot, Response
from microdot.websocket import with_websocket
from setup import check_registration
import jobs.ingest as _ingest
from jobs.ingest import ingest_loop
from services.sensor import extract_metric
import state

load_dotenv()

PORT     = int(os.getenv("PORT", 8080))
NICKNAME = os.getenv("DEVICE_NICKNAME", socket.gethostname())

app = Microdot()

STATIC_DIR = Path(__file__).parent / "static"


# ----------------------- Static helpers -----------------------

def _serve_file(path: str, content_type: str) -> Response:
    try:
        with open(path, "rb") as f:
            return Response(body=f.read(), headers={"Content-Type": content_type})
    except FileNotFoundError:
        return Response(status_code=404)


# ----------------------- Routes -----------------------

@app.get("/")
async def index(request):
    return _serve_file(STATIC_DIR / "dashboard.html", "text/html; charset=utf-8")


@app.get("/static/<path:path>")
async def static_files(request, path):
    # Prevent path traversal
    resolved = (STATIC_DIR / path).resolve()
    if not str(resolved).startswith(str(STATIC_DIR.resolve())):
        return Response(status_code=403)
    ext = path.rsplit(".", 1)[-1].lower()
    ct = {"js": "text/javascript", "css": "text/css", "html": "text/html"}.get(ext, "application/octet-stream")
    return _serve_file(resolved, ct)


@app.get("/health")
async def health(request):
    return {"status": "ok"}


@app.post("/re-register")
async def re_register(request):
    """Start the registration wizard (called from dashboard "Re-register" button)."""
    check = await asyncio.create_subprocess_exec(
        "systemctl", "is-active", "--quiet", "schoolair-wizard",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await check.wait()
    already_up = check.returncode == 0

    if not already_up:
        start = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "start", "schoolair-wizard",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await start.wait()

    host = (request.headers.get("Host") or "").split(":")[0]
    return {"url": f"http://{host}/", "already_up": already_up}


@app.route("/ws/sensors")
@with_websocket
async def ws_sensors(request, ws):
    """Push the latest sensor reading to the dashboard every 30 s."""
    while True:
        frame = {
            "temp":       extract_metric(state.latest_data, "temp") if state.latest_data else None,
            "pm25":       extract_metric(state.latest_data, "pm25") if state.latest_data else None,
            "registered": bool(os.getenv("AUTH_TOKEN", "").strip()),
            "nickname":   NICKNAME,
            "sent_at":    state.latest_recorded_at,
        }
        await ws.send(json.dumps(frame))
        await asyncio.sleep(30)


# ----------------------- Signal handlers -----------------------

async def _graceful_shutdown():
    """SIGTERM: flush in-flight data to SQLite, then cancel all tasks.

    Covers systemctl stop/restart and OS shutdown (reboot/poweroff).
    Does NOT cover SIGKILL or physical power loss.
    """
    queue.init()
    ts = datetime.now(timezone.utc).isoformat()

    if _ingest._pending_live:
        item = _ingest._pending_live
        queue.enqueue(item["data"], item["recorded_at"])
        _ingest._pending_live = None
        print("[shutdown] Flushed 1 unsent live reading to SQLite before exit")

    if _ingest._alert_buffer:
        n = len(_ingest._alert_buffer)
        for a in _ingest._alert_buffer:
            queue.enqueue_alert(a, a.get("recorded_at", ts))
        _ingest._alert_buffer.clear()
        print(f"[shutdown] Flushed {n} alert(s) to SQLite before exit")

    current = asyncio.current_task()
    for t in asyncio.all_tasks():
        if t is not current:
            t.cancel()


# ----------------------- Entry point -----------------------

async def main():
    loop = asyncio.get_event_loop()

    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(_graceful_shutdown()))

    await asyncio.gather(
        ingest_loop(),
        app.start_server(host="0.0.0.0", port=PORT, debug=False),
    )


if __name__ == "__main__":
    check_registration()  # logs if no token; does not exit
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

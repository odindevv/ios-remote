"""WDA falso para probar ios-remote sin teléfono.

    python tests/fake_wda.py                 # HTTP 8100, video 9100
    python tests/fake_wda.py 18100 19100     # otros puertos (lo usa tests/selftest.py)

Imita lo que importa del WDA real: sesión única, /wda/homescreen sin sesión, /wda/tap/0 que
no existe en versiones nuevas, ajustes globales (/appium/settings), batería, captura y el
video MJPEG. Guarda cada comando recibido en GET /calls para que los tests lo revisen.
"""
import asyncio
import base64
import io
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw

HTTP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
MJPEG_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9100

app = FastAPI()
SID = "S1"
CALLS = []
STATE = {"orient": "PORTRAIT", "settings": {"waitForIdleTimeout": 10, "animationCoolOffTimeout": 2}}


def ok(v=None):
    return {"value": v, "sessionId": SID}


@app.get("/status")
async def status():
    return {"value": {"ready": True, "os": {"version": "16.7.16"}, "ios": {"ip": "192.168.1.50"},
                      "build": {"version": "fake"}}, "sessionId": SID}


@app.post("/session")
async def new_session():
    return {"value": {"sessionId": SID}, "sessionId": SID}


@app.post("/wda/homescreen")
async def homescreen():
    CALLS.append(["homescreen", None])
    return ok()


@app.get("/screenshot")
async def screenshot():
    im = Image.new("RGB", (750, 1334), (30, 90, 160))
    b = io.BytesIO()
    im.save(b, "PNG")
    return ok(base64.b64encode(b.getvalue()).decode())


@app.post("/session/{sid}/wda/tap/0")
async def tap_old(sid: str):  # como en WDA nuevo: esta ruta ya no existe
    return JSONResponse({"value": {"error": "unknown command", "message": "Unhandled endpoint"}}, 404)


@app.api_route("/session/{sid}/{rest:path}", methods=["GET", "POST"])
async def in_session(sid: str, rest: str, req: Request):
    if sid != SID:
        return JSONResponse({"value": {"error": "invalid session id", "message": "deleted"}}, 404)
    body = None
    if req.method == "POST":
        try:
            body = await req.json()
        except Exception:
            body = None
    CALLS.append([rest, body])
    if rest == "window/size":
        portrait = STATE["orient"] == "PORTRAIT"
        return ok({"width": 375, "height": 812} if portrait else {"width": 812, "height": 375})
    if rest == "orientation":
        if body:
            STATE["orient"] = body["orientation"]
        return ok(STATE["orient"])
    if rest == "appium/settings":
        if body:
            STATE["settings"].update(body.get("settings", {}))
        return ok(dict(STATE["settings"]))
    if rest == "wda/locked":
        return ok(False)
    if rest == "wda/batteryInfo":
        return ok({"level": 0.76, "state": 2})
    return ok()


@app.get("/calls")
async def calls():
    return CALLS


@app.get("/settings")
async def settings():
    return STATE["settings"]


def frame(i: int) -> bytes:
    im = Image.new("RGB", (188, 406), (20, 20, 40))
    ImageDraw.Draw(im).text((10, 10), f"frame {i}", fill="white")
    b = io.BytesIO()
    im.save(b, "JPEG")
    return b.getvalue()


async def mjpeg(reader, writer):
    await reader.read(1024)
    writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=--BoundaryString\r\n\r\n")
    i = 0
    try:
        while True:
            f = frame(i)
            i += 1
            writer.write(b"--BoundaryString\r\nContent-type: image/jpg\r\nContent-Length: %d\r\n\r\n" % len(f)
                         + f + b"\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(1 / 20)
    except Exception:
        pass


async def main():
    srv = await asyncio.start_server(mjpeg, "127.0.0.1", MJPEG_PORT)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=HTTP_PORT, log_level="warning")
    await asyncio.gather(srv.serve_forever(), uvicorn.Server(cfg).serve())


if __name__ == "__main__":
    asyncio.run(main())

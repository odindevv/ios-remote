"""ios-remote (versión WDA) — servidor.

    python server.py            # usa config.json
    python server.py --no-tidevice   # si ya tenés xctest/relay corriendo a mano

Arquitectura (comparada con DimCyan/iOS-remote):
  navegador ──WebSocket──► server.py ──HTTP keep-alive──► relay ─USB─► WDA :8100
            ◄─frames JPEG─           ◄──── 1 conexión MJPEG ── relay ─USB─► WDA :9100
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from mjpeg import MjpegHub
from wda_client import WDA, WDAError

ROOT = Path(__file__).parent
log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)  # no loguear cada request a WDA

def _config_path() -> Path:
    """--config <archivo> (o --config=<archivo>) elige otra configuración."""
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--config", default="config.json")
    ns, _ = ap.parse_known_args()
    return (ROOT / ns.config).resolve()


CONFIG_PATH = _config_path()
# Sin config.json también funciona: los iPhones se detectan solos por USB ("auto").
CFG = (json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists()
       else {"host": "127.0.0.1", "port": 5000, "token": "", "devices": []})
CFG.setdefault("devices", [])
TOKEN = CFG.get("token") or ""


class Device:
    def __init__(self, d: dict):
        self.cfg = d
        self.id = d["id"]
        g = CFG.get("gestures", {})
        self.wda = WDA(f"http://127.0.0.1:{d['wda_port']}",
                       swipe_max_ms=g.get("swipe_max_ms", 220), swipe_mode=g.get("swipe_mode", "auto"),
                       share_session=d.get("share_session", False))
        # Sin "mjpeg_port": el video va directo por USB, sin relay ni puerto abierto en la PC.
        self.hub = MjpegHub("127.0.0.1", d.get("mjpeg_port"), udid=d.get("udid") or None)
        self.stream_sid = None  # sesión en la que ya se aplicaron los ajustes del stream
        self.cmd_lock = asyncio.Lock()  # los gestos de varios visores no se pisan
        self.speed_lock = asyncio.Lock()  # speed_on/off nunca se cruzan (recargar la página)

    async def sync_settings(self):
        """Deja WDA como corresponde a quién está mirando: con visores → video configurado y
        sin esperas; sin visores → ajustes originales. Bajo un lock, así un 'apagar' de la
        pestaña vieja y un 'prender' de la nueva no se pisan al recargar."""
        async with self.speed_lock:
            if self.hub.viewers:
                await self.apply_stream_settings()
                await self.wda.speed_on()
            else:
                await self.wda.speed_off()

    def settings_stale(self) -> bool:
        """¿Hay que (re)aplicar los ajustes? (primera vez falló, o WDA se reinició)."""
        return (not self.cfg.get("share_session")) and (not self.wda._fast or self.stream_sid != self.wda.sid)

    async def apply_stream_settings(self):
        if self.cfg.get("share_session"):
            return  # no tocar los ajustes de un WDA que usa otro cliente
        sid = await self.wda.ensure_session()
        if self.stream_sid == sid:
            return
        s = self.cfg.get("stream", {})
        await self.wda.stream_settings(s.get("fps", 30), s.get("scale", 50), s.get("quality", 40))
        self.stream_sid = sid


DEVICES = {d["id"]: Device(d) for d in CFG["devices"]}
for _d in DEVICES.values():
    _d.connected = None          # None = todavía no se escaneó el USB

# ---------- detección automática de iPhones por USB ----------
AUTO = CFG.get("auto", True)                    # "auto": false en la config la apaga
CAN_LAUNCH = True                               # main() lo pone en False con --no-tidevice
DETECTED: dict[str, dict] = {}                  # udid -> lo que se vio en el último escaneo
_CONFIG_UDIDS = {d.get("udid") for d in CFG["devices"] if d.get("udid")}
_EXCLUDE = set(CFG.get("auto_exclude", []))


# ---------- teléfonos conocidos (se recuerdan aunque estén desenchufados) ----------
_KNOWN_FILE = ROOT / "cache" / "known_devices.json"
try:
    KNOWN: dict[str, dict] = json.loads(_KNOWN_FILE.read_text(encoding="utf-8"))
except Exception:
    KNOWN = {}
_known_saved = 0.0


def _save_known(force: bool = False):
    """Se guarda al ver un teléfono nuevo o cambiado, y la hora 'visto por última vez' cada minuto."""
    global _known_saved
    if not force and time.time() - _known_saved < 60:
        return
    try:
        _KNOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _KNOWN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(KNOWN, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_KNOWN_FILE)
        _known_saved = time.time()
    except OSError as e:
        log.warning("no pude guardar %s: %s", _KNOWN_FILE.name, e)


import importlib.util as _ilu
_HAS_PMD = _ilu.find_spec("pymobiledevice3") is not None   # obligatorio para iOS 17+


def _free_port() -> int:
    used = {d.cfg["wda_port"] for d in DEVICES.values()}
    p = int(CFG.get("auto_port_base", 8200))   # 8200+: lejos del 8100/8101 que usa el vigía
    while p in used:
        p += 1
    return p


def _auto_cfg(info: dict) -> dict:
    stream = CFG.get("auto_stream", {"fps": 25, "scale": 50, "quality": 40})
    return {"id": info["udid"][-6:].upper(), "name": info["name"], "udid": info["udid"], "ios": info.get("ios"),
            "bundle_id": info["bundle"], "wda_port": _free_port(), "manage_tidevice": True,
            "stream": stream, "auto": True}


def _print_scan(found: list[dict]):
    print("\n  iPhones on USB:")
    if not found:
        print("    (none yet — plug one in and unlock it; it will show up by itself)")
    for f in found:
        line = f"    • {f.get('name', '?'):<22} iOS {f.get('ios') or '?':<8} {f['udid'] or ''}"
        print(line + f"\n        → {f['status']}")
    print(f"\n  Open http://127.0.0.1:{CFG.get('port', 5000)}/  to use them.\n", flush=True)


async def auto_loop():
    from discover import scan, ios_major
    from mjpeg import in_daemon_thread
    first = True
    while True:
        try:
            found = await asyncio.wait_for(in_daemon_thread(scan), 40)
        except Exception as e:
            log.warning("escaneo USB falló: %s", e)
            found = []
        seen = set()
        for f in found:
            udid = f.get("udid")
            if not udid:
                continue
            seen.add(udid)
            if not f.get("problem"):   # recordarlo, aunque después se desenchufe
                prev = KNOWN.get(udid, {})
                now_info = {"name": f.get("name"), "ios": f.get("ios"), "model": f.get("model")}
                changed = any(prev.get(k) != v for k, v in now_info.items())
                KNOWN[udid] = {**prev, **now_info, "last_seen": time.time()}
                _save_known(force=changed)
            if not f.get("problem"):   # íconos de todas sus apps a disco, en segundo plano
                from icons import prefetch
                asyncio.create_task(prefetch(udid, f.get("name", "")))
            dev = next((d for d in DEVICES.values() if d.cfg.get("udid") == udid), None)
            if dev:
                f["status"], f["id"] = "ready to use", dev.id
            elif udid in _CONFIG_UDIDS or udid in _EXCLUDE:
                f["status"] = "skipped (listed in config / excluded)"
            elif f.get("problem"):
                f["status"] = f["problem"]
            elif not f.get("bundle"):
                f["status"] = "WebDriverAgent not installed on this phone"
            elif ios_major(f.get("ios")) >= 17 and not _HAS_PMD:
                f["status"] = "iOS 17+ needs pymobiledevice3 — run SETUP.bat"
            elif not CAN_LAUNCH:
                f["status"] = "found (starting is off: --no-tidevice)"
            else:
                f["status"], f["startable"] = "ready — press Start on the Phones page", True
            DETECTED[udid] = f
        for d in DEVICES.values():
            if d.cfg.get("udid"):
                d.connected = d.cfg["udid"] in seen
        for udid in list(DETECTED):
            if udid not in seen:
                DETECTED.pop(udid)
        if first:
            _print_scan(found)
            first = False
        await asyncio.sleep(8)
def _quiet_win_reset(loop, ctx):
    # Bug conocido de asyncio en Windows: cuando el navegador cierra o recarga la pestaña,
    # el cierre del socket tira ConnectionResetError (WinError 10054). Es inofensivo.
    if isinstance(ctx.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(ctx)


@asynccontextmanager
async def lifespan(_app):
    asyncio.get_running_loop().set_exception_handler(_quiet_win_reset)
    scanner = asyncio.create_task(auto_loop()) if AUTO else None
    from icons import prefetch
    for d in list(DEVICES.values()):
        if d.cfg.get("udid"):
            asyncio.create_task(prefetch(d.cfg["udid"], d.cfg.get("name", "")))
    yield
    if scanner:
        scanner.cancel()
    _save_known(force=True)
    for d in DEVICES.values():   # al apagar: los ajustes globales de WDA vuelven como estaban
        await d.wda.speed_off()


app = FastAPI(lifespan=lifespan)
if CFG.get("host", "127.0.0.1") in ("127.0.0.1", "localhost"):
    # Sólo se atiende a "127.0.0.1"/"localhost": bloquea el truco de DNS rebinding.
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


def _authorized(token: str | None) -> bool:
    return not TOKEN or (token is not None and secrets.compare_digest(token, TOKEN))


def _page(name: str, request: Request, token: str | None):
    if not _authorized(token or request.cookies.get("tok")):
        return Response("Missing ?token=…", status_code=401)
    resp = FileResponse(ROOT / "static" / name)
    if token:
        resp.set_cookie("tok", token, httponly=True, samesite="strict", max_age=30 * 86400)
    return resp


@app.get("/")
async def dashboard(request: Request, token: str | None = None):
    """Inicio: todos los teléfonos con su miniatura; se elige cuál controlar."""
    return _page("dashboard.html", request, token)


@app.get("/control")
async def control(request: Request, token: str | None = None):
    """Panel de control de UN teléfono (/control?dev=A)."""
    return _page("index.html", request, token)


async def _device_info(d: "Device") -> dict:
    info = {"id": d.id, "name": d.cfg.get("name", d.id), "udid": d.cfg.get("udid", ""),
            "wda": False, "ios": None, "ip": None, "battery": None, "charging": None,
            "viewers": d.hub.viewers, "fps": round(d.hub.fps, 1),
            "usb": getattr(d, "connected", None), "auto": bool(d.cfg.get("auto")),
            "starting": time.time() - getattr(d, "started_at", 0) < 90}
    try:
        st = await asyncio.wait_for(d.wda.status(), 3) or {}
        info["wda"] = bool(st.get("ready", True))
        info["ios"] = (st.get("os") or {}).get("version")
        info["ip"] = (st.get("ios") or {}).get("ip")
    except Exception:
        return info
    try:  # batería: necesita sesión; se reutiliza la abierta (no le quita la suya a nadie)
        sid = await asyncio.wait_for(d.wda.ensure_session(), 4)
        b = await asyncio.wait_for(d.wda._req("GET", f"/session/{sid}/wda/batteryInfo"), 3) or {}
        if b.get("level", -1) >= 0:
            info["battery"] = round(b["level"] * 100)
            info["charging"] = b.get("state") in (2, 3)   # 2 = cargando, 3 = llena enchufada
    except Exception:
        pass
    return info


@app.get("/api/devices")
async def devices(request: Request):
    if not _authorized(request.cookies.get("tok")):
        raise HTTPException(401)
    return await asyncio.gather(*(_device_info(d) for d in DEVICES.values()))


def _same_site(request: Request) -> bool:
    """Los botones Start/Stop sólo valen desde nuestras propias páginas."""
    from urllib.parse import urlparse
    o = request.headers.get("origin")
    return o is None or urlparse(o).netloc == request.headers.get("host")


@app.post("/api/start/{udid}")
async def start_phone(udid: str, request: Request):
    """Botón Start: arranca WebDriverAgent en un iPhone detectado."""
    if not _authorized(request.cookies.get("tok")) or not _same_site(request):
        raise HTTPException(403)
    f = DETECTED.get(udid)
    if not f or not f.get("startable"):
        raise HTTPException(409, (f or {}).get("status") or "phone not found")
    cfg = _auto_cfg(f)
    d = Device(cfg)
    d.connected = True
    d.started_at = time.time()
    DEVICES[cfg["id"]] = d
    from supervisor import start_for_device
    d.procs = await asyncio.to_thread(start_for_device, cfg)
    f.pop("startable", None)
    f["status"], f["id"] = f"started (port {cfg['wda_port']})", cfg["id"]
    log.info("Start: %s (%s) → WDA en el puerto %s", f["name"], udid, cfg["wda_port"])
    return {"id": cfg["id"]}


@app.post("/api/stop/{dev_id}")
async def stop_phone(dev_id: str, request: Request):
    """Botón Stop: apaga WebDriverAgent de un teléfono que se arrancó con Start."""
    if not _authorized(request.cookies.get("tok")) or not _same_site(request):
        raise HTTPException(403)
    d = DEVICES.get(dev_id) or _404()
    if not d.cfg.get("auto"):
        raise HTTPException(409, "this phone is managed by the config file")
    for pr in getattr(d, "procs", []):
        await asyncio.to_thread(pr.kill)
    DEVICES.pop(dev_id, None)
    f = DETECTED.get(d.cfg["udid"])
    if f:
        f.pop("id", None)
        f["status"], f["startable"] = "ready — press Start on the Phones page", True
    log.info("Stop: %s", d.cfg.get("name"))
    return {"ok": True}


@app.get("/api/detected")
async def detected(request: Request):
    """iPhones vistos por USB que NO se pueden usar todavía, con el motivo."""
    if not _authorized(request.cookies.get("tok")):
        raise HTTPException(401)
    out = [{"udid": u, "name": f.get("name"), "ios": f.get("ios"), "status": f.get("status"),
            "startable": bool(f.get("startable"))}
           for u, f in DETECTED.items() if not f.get("id")]
    # registrados que ahora no están conectados (y que no se ven ya como tarjeta activa)
    active = {d.cfg.get("udid") for d in DEVICES.values()}
    for u, k in sorted(KNOWN.items(), key=lambda kv: -kv[1].get("last_seen", 0)):
        if u not in DETECTED and u not in active and u not in _CONFIG_UDIDS:
            out.append({"udid": u, "name": k.get("name"), "ios": k.get("ios"), "offline": True,
                        "last_seen": k.get("last_seen"), "status": "Not connected"})
    return out


@app.post("/api/forget/{udid}")
async def forget_phone(udid: str, request: Request):
    """Botón Forget: saca un teléfono desconectado de la lista de registrados."""
    if not _authorized(request.cookies.get("tok")) or not _same_site(request):
        raise HTTPException(403)
    if KNOWN.pop(udid, None) is not None:
        _save_known(force=True)
    return {"ok": True}


_THUMBS: dict[str, tuple[float, bytes, str]] = {}


@app.get("/api/{dev_id}/thumb")
async def thumb(dev_id: str, request: Request):
    """Miniatura de la pantalla para el inicio. Si alguien está mirando ese teléfono se usa
    el último frame del video (no cuesta nada); si no, una captura de WDA, guardada 10 s
    para no cargar al teléfono."""
    if not _authorized(request.cookies.get("tok")):
        raise HTTPException(401)
    d = DEVICES.get(dev_id) or _404()
    if d.hub.viewers and d.hub.frame:
        return Response(d.hub.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    cached = _THUMBS.get(dev_id)
    if cached and time.time() - cached[0] < 10:
        return Response(cached[1], media_type=cached[2], headers={"Cache-Control": "no-store"})
    try:
        png = base64.b64decode(await asyncio.wait_for(d.wda.screenshot_png_b64(), 10))
    except Exception:
        raise HTTPException(503, "WDA not responding")
    data, mime = png, "image/png"
    try:  # achicarla (viene con Pillow, que instala tidevice); si no está, va el PNG entero
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        im.thumbnail((360, 780))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        data, mime = buf.getvalue(), "image/jpeg"
    except Exception:
        pass
    _THUMBS[dev_id] = (time.time(), data, mime)
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/api/{dev_id}/screenshot")
async def screenshot(dev_id: str, request: Request):
    if not _authorized(request.cookies.get("tok")):
        raise HTTPException(401)
    d = DEVICES.get(dev_id) or _404()
    try:
        png = base64.b64decode(await asyncio.wait_for(d.wda.screenshot_png_b64(), 15))
    except Exception:
        raise HTTPException(503, "WDA not responding")
    name = time.strftime(f"{dev_id}-%Y%m%d-%H%M%S.png")
    return Response(png, media_type="image/png",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/{dev_id}/apps")
async def open_apps(dev_id: str, request: Request):
    """Apps abiertas en el teléfono (para el selector de apps del panel)."""
    if not _authorized(request.cookies.get("tok")):
        raise HTTPException(401)
    d = DEVICES.get(dev_id) or _404()
    from apps import running_apps
    from mjpeg import in_daemon_thread
    from supervisor import ios_major
    major = ios_major(d.cfg)
    if major and not d.cfg.get("ios"):
        d.cfg["ios"] = str(major)

    async def fresh():
        apps = await asyncio.wait_for(in_daemon_thread(running_apps, d.cfg.get("udid"), major), 50)
        _APPS[dev_id] = (time.time(), apps)
        return apps
    # iOS 17+ tarda unos segundos (arma el túnel): se devuelve al instante la última lista y se
    # actualiza por detrás; ?fresh=1 (botón Actualizar) espera la lista nueva.
    cached = _APPS.get(dev_id)
    if major >= 17 and cached and not request.query_params.get("fresh"):
        if time.time() - cached[0] > 3 and dev_id not in _APPS_BUSY:
            _APPS_BUSY.add(dev_id)
            asyncio.create_task(fresh()).add_done_callback(lambda _t: _APPS_BUSY.discard(dev_id))
        return cached[1]
    try:
        return await fresh()
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


_APPS: dict[str, tuple[float, list]] = {}
_APPS_BUSY: set[str] = set()


_ICON_DIR = ROOT / "cache" / "icons"
_ICON_SEM = asyncio.Semaphore(2)          # no lanzar 10 pymobiledevice3 a la vez
_BUNDLE_OK = __import__("re").compile(r"^[A-Za-z0-9._-]{1,200}$")


@app.get("/api/{dev_id}/icon/{bundle}")
async def app_icon(dev_id: str, bundle: str, request: Request):
    """Ícono real de la app. La primera vez se le pide al teléfono (pymobiledevice3) y se
    guarda en cache/icons/; después sale del disco al instante."""
    if not _authorized(request.cookies.get("tok")):
        raise HTTPException(401)
    d = DEVICES.get(dev_id) or _404()
    if not _BUNDLE_OK.match(bundle):
        raise HTTPException(400)
    path = _ICON_DIR / f"{bundle}.png"
    if not path.exists():
        _ICON_DIR.mkdir(parents=True, exist_ok=True)
        udid = ["--udid", d.cfg["udid"]] if d.cfg.get("udid") else []
        async with _ICON_SEM:
            if not path.exists():
                try:  # conexión directa al teléfono: rápido (no arranca otro programa)
                    from icons import fetch_one
                    await asyncio.wait_for(fetch_one(d.cfg.get("udid") or None, bundle), 10)
                except Exception:
                    pass
            if not path.exists():
                # subprocess.run en un hilo (no create_subprocess_exec): así no depende del
                # tipo de event loop que use uvicorn en Windows.
                import subprocess
                cmd = [sys.executable, "-m", "pymobiledevice3", "springboard", "icon",
                       bundle, str(path), *udid]
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                try:
                    await asyncio.to_thread(subprocess.run, cmd, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, timeout=25,
                                            creationflags=flags)
                except Exception:
                    pass
    if not path.exists() or path.stat().st_size == 0:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "max-age=604800, immutable"})


def _404():
    raise HTTPException(404, "unknown device")


async def run_cmd(d: Device, m: dict):
    w, op = d.wda, m.get("op")
    if op == "tap":
        return await w.tap(m["x"], m["y"])
    if op == "hold":
        return await w.long_press(m["x"], m["y"], int(m.get("ms", 800)))
    if op == "swipe":
        return await w.swipe(m["path"])
    if op == "keys":
        return await w.keys(m["text"])
    if op == "home":
        return await w.home()
    if op == "switcher":
        return await w.app_switcher()
    if op == "button":
        return await w.button(m["name"])
    if op == "lock":
        return await (w.unlock() if await w.locked() else w.lock())
    if op == "rotate":
        return await w.rotate()
    if op == "activate":
        return await w.activate(m["bundle"])
    if op == "terminate":
        return await w.terminate(m["bundle"])
    if op == "launch":
        return await w.launch(m["bundle"])
    if op == "size":
        return await w.window_size()
    raise ValueError(f"unknown command: {op}")


def _same_origin(sock: WebSocket) -> bool:
    """El WebSocket sólo se acepta desde las páginas de este mismo servidor. Sin esto,
    cualquier sitio abierto en el navegador podría conectarse a ws://127.0.0.1:5000 y
    controlar el teléfono."""
    from urllib.parse import urlparse
    o = sock.headers.get("origin")
    return o is None or urlparse(o).netloc == sock.headers.get("host")


@app.websocket("/ws/{dev_id}")
async def ws(sock: WebSocket, dev_id: str):
    if not _authorized(sock.cookies.get("tok")) or dev_id not in DEVICES or not _same_origin(sock):
        await sock.close(code=4401)
        return
    d = DEVICES[dev_id]
    await sock.accept()
    d.hub.acquire()
    try:
        await d.sync_settings()
    except Exception as e:
        log.warning("no se pudieron aplicar ajustes (se reintenta con el próximo comando): %s", e)

    async def pump_frames():
        seq = 0
        while True:
            seq, frame = await d.hub.next_frame(seq)
            if frame:
                try:
                    await sock.send_bytes(frame)
                except (RuntimeError, WebSocketDisconnect):
                    return

    pump = asyncio.create_task(pump_frames())
    try:
        while True:
            m = json.loads(await sock.receive_text())
            t0 = time.perf_counter()
            if d.settings_stale():   # WDA no estaba listo al conectar, o se reinició
                try:
                    await d.sync_settings()
                except Exception:
                    pass
            try:
                async with d.cmd_lock:
                    res = await run_cmd(d, m)
                reply = {"id": m.get("id"), "ok": True, "res": res}
            except (WDAError, ValueError, KeyError) as e:
                reply = {"id": m.get("id"), "ok": False, "err": str(e)}
            except Exception as e:  # relay caído → httpx.ConnectError, etc.
                reply = {"id": m.get("id"), "ok": False, "err": f"{type(e).__name__}: {e}"}
            reply["ms"] = round((time.perf_counter() - t0) * 1000)
            log.info("%s %s → %s (%d ms)", dev_id, m.get("op"),
                     "ok" if reply["ok"] else reply["err"], reply["ms"])
            try:
                await sock.send_text(json.dumps(reply, default=str))
            except (RuntimeError, WebSocketDisconnect):
                break  # el navegador cerró o recargó mientras el comando corría
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        d.hub.release()
        if d.hub.viewers == 0:   # el último que miraba se fue: WDA vuelve como estaba
            try:
                await d.sync_settings()
            except Exception:
                pass




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json", help="archivo de configuración")
    ap.add_argument("--no-tidevice", action="store_true",
                    help="no lanzar xctest/relay (ya los corrés a mano)")
    a = ap.parse_args()
    global CAN_LAUNCH
    CAN_LAUNCH = not a.no_tidevice
    log.info("configuración: %s%s", CONFIG_PATH.name, "" if CONFIG_PATH.exists() else " (no existe: sólo detección automática)")
    from supervisor import bind_children_to_this_process
    bind_children_to_this_process()
    if not a.no_tidevice:
        from supervisor import start_for_device
        n = len(CFG["devices"])
        for dev in CFG["devices"]:
            mode = dev.get("manage_tidevice", True)
            if not mode:
                continue
            if n > 1 and not dev.get("udid"):
                log.error("teléfono %s: con más de un teléfono hace falta su udid; no lanzo tidevice",
                          dev["id"])
                continue
            start_for_device(dev, video_only=(mode == "video"))
    if not TOKEN and CFG.get("host", "127.0.0.1") != "127.0.0.1":
        log.warning("⚠ escuchando en %s SIN token: cualquiera en la red controla el teléfono",
                    CFG["host"])
    try:
        uvicorn.run(app, host=CFG.get("host", "127.0.0.1"), port=CFG.get("port", 5000),
                    log_level="warning")
    finally:
        from supervisor import stop_all
        stop_all()   # Ctrl+C: cerrar los tidevice que lanzó ESTE servidor (no los del vigía)


if __name__ == "__main__":
    main()

"""Cliente WDA asíncrono, mínimo y tolerante.

Robustez:
  * Una ruta puede no existir según la versión de WDA → se prueban variantes en orden
    y se RECUERDA la que funcionó (tap, home).
  * /wda/homescreen es ruta SIN sesión.
  * Sesión muerta (404 / "invalid session id") → se recrea una vez y se reintenta.
Coordenadas: todo entra como FRACCIÓN (0..1) de la pantalla y se convierte a PUNTOS
con /window/size, así el navegador no necesita saber la resolución.
"""
from __future__ import annotations

import asyncio
import logging
import math

import httpx

log = logging.getLogger("wda")


class WDAError(Exception):
    pass


class WDA:
    def __init__(self, base_url: str, timeout: float = 15.0,
                 swipe_max_ms: int = 220, swipe_mode: str = "auto", share_session: bool = False):
        self.base = base_url.rstrip("/")
        # trust_env=False: en Windows httpx leería el proxy del sistema y podría mandar
        # 127.0.0.1 a través de él. Keep-alive: una sola conexión TCP reutilizada.
        self.http = httpx.AsyncClient(base_url=self.base, timeout=timeout, trust_env=False,
                                      limits=httpx.Limits(max_keepalive_connections=4))
        self.swipe_max_ms = swipe_max_ms
        self.swipe_mode = swipe_mode
        # share_session: otro programa usa este WDA: no se cambia NINGÚN ajuste global
        # (esperas, video). La sesión abierta se reutiliza siempre, con o sin esto.
        self.share = share_session
        self._orig: dict | None = None   # ajustes globales de WDA antes de tocarlos
        self._fast = False
        self.sid: str | None = None
        self.size: tuple[float, float] | None = None  # puntos (w, h)
        self._lock = asyncio.Lock()
        self._route: dict[str, str] = {}  # acción -> variante que funcionó
        self._bad: dict[str, set] = {}    # acción -> variantes que colgaron a WDA

    # ---------- bajo nivel ----------
    async def _req(self, method: str, path: str, body=None):
        r = await self.http.request(method, path, json=body)
        try:
            data = r.json()
        except ValueError:
            data = {}
        val = data.get("value") if isinstance(data, dict) else None
        if r.status_code >= 400 or (isinstance(val, dict) and "error" in val):
            msg = val.get("message", r.text[:200]) if isinstance(val, dict) else r.text[:200]
            err = val.get("error", "") if isinstance(val, dict) else ""
            raise WDAError(f"{r.status_code} {err} {msg}".strip())
        return val

    async def status(self):
        return await self._req("GET", "/status")

    async def ensure_session(self, force: bool = False) -> str:
        async with self._lock:
            if self.sid and not force:
                return self.sid
            # WDA tiene UNA sola sesión. Crear otra mata la de quien la esté usando (el bench,
            # otro panel, un flujo) y ese otro crea una nueva y mata la nuestra: un ping-pong
            # en el que cada comando paga 1-2 s de sesión nueva o muere a medio camino.
            # Por eso: si WDA ya tiene una sesión abierta (y no es la que se nos acaba de
            # morir), se usa ésa. Sólo se crea una cuando no hay ninguna.
            dead = self.sid if force else None
            try:
                st = await self.http.get("/status")
                sid = (st.json() or {}).get("sessionId")
            except Exception:
                sid = None
            if sid and sid != dead:
                if sid != self.sid:
                    self.sid, self.size = sid, None
                    log.info("usando la sesión abierta de WDA %s", sid)
                return sid
            # Sólo lo que es DE LA SESIÓN. Las esperas (waitForIdleTimeout, etc.) son
            # GLOBALES del WDA y sobreviven a la sesión: se ponen con speed_on() y se
            # devuelven a su valor original con speed_off().
            caps = {"shouldWaitForQuiescence": False}
            val = await self.http.post("/session", json={"capabilities": {"alwaysMatch": caps}})
            data = val.json()
            sid = data.get("sessionId") or (data.get("value") or {}).get("sessionId")
            if not sid:
                raise WDAError(f"could not create a WDA session: {str(data)[:200]}")
            self.sid = sid
            self.size = None
            log.info("sesión WDA %s", sid)
            return sid

    # ---------- ajustes globales de velocidad ----------
    FAST = {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}

    async def speed_on(self):
        """Quita las esperas de WDA (que la UI se calme, que terminen las animaciones).
        Son ajustes GLOBALES: antes de cambiarlos se guardan los que había, para que
        speed_off() los devuelva y otro programa que use este WDA no herede los nuestros."""
        if self.share or self._fast:
            return
        sid = await self.ensure_session()
        try:
            if self._orig is None:
                cur = await self._req("GET", f"/session/{sid}/appium/settings") or {}
                self._orig = {k: cur[k] for k in self.FAST if k in cur}
                log.info("ajustes originales de WDA: %s", self._orig)
            await self._req("POST", f"/session/{sid}/appium/settings", {"settings": self.FAST})
            self._fast = True
        except Exception as e:
            log.info("ajustes de velocidad no aplicados: %s", e)

    async def speed_off(self):
        if not self._fast:
            return
        self._fast = False
        if not self._orig:
            return
        try:
            sid = await self.ensure_session()
            await self._req("POST", f"/session/{sid}/appium/settings", {"settings": self._orig})
            log.info("ajustes de WDA restaurados: %s", self._orig)
        except Exception as e:
            log.warning("no pude restaurar los ajustes de WDA: %s", e)

    async def s(self, method: str, path: str, body=None):
        """Request con sesión; recrea la sesión una vez si murió."""
        sid = await self.ensure_session()
        try:
            return await self._req(method, f"/session/{sid}{path}", body)
        except WDAError as e:
            if "invalid session" in str(e).lower() or str(e).startswith("404 invalid"):
                self._fast = False   # WDA pudo haberse reiniciado: vuelve con ajustes de fábrica
                sid = await self.ensure_session(force=True)
                return await self._req(method, f"/session/{sid}{path}", body)
            raise

    async def _first_ok(self, key: str, variants: list):
        """variants: [(nombre, corrutina_factory)]. Usa la recordada primero."""
        known = self._route.get(key)
        bad = self._bad.setdefault(key, set())
        ordered = sorted((v for v in variants if v[0] not in bad), key=lambda v: v[0] != known)
        if not ordered:  # todas fallaron alguna vez: se vuelve a probar desde cero
            bad.clear()
            ordered = list(variants)
        last = None
        for name, fn in ordered:
            try:
                res = await fn()
                if known != name:
                    self._route[key] = name
                    log.info("ruta %s -> %s", key, name)
                return res
            except WDAError as e:
                last = e
            except httpx.TimeoutException:
                # WDA se colgó con esta variante. NO se prueba la siguiente ahora: el gesto
                # podría ejecutarse igual más tarde y saldría doble. Se marca como mala y el
                # PRÓXIMO gesto ya usa otra.
                bad.add(name)
                if self._route.get(key) == name:
                    del self._route[key]
                log.warning("%s: '%s' se colgó en WDA; el próximo usa otra variante", key, name)
                raise
        raise last or WDAError(f"{key}: no working variant")

    # ---------- geometría ----------
    async def window_size(self) -> tuple[float, float]:
        if not self.size:
            v = await self.s("GET", "/window/size")
            self.size = (float(v["width"]), float(v["height"]))
        return self.size

    async def pt(self, fx: float, fy: float) -> tuple[int, int]:
        w, h = await self.window_size()
        fx = min(max(fx, 0.0), 1.0)
        fy = min(max(fy, 0.0), 1.0)
        return round(fx * w), round(fy * h)

    # ---------- gestos ----------
    async def tap(self, fx: float, fy: float):
        x, y = await self.pt(fx, fy)
        return await self._first_ok("tap", [
            ("wda/tap", lambda: self.s("POST", "/wda/tap", {"x": x, "y": y})),
            ("wda/tap/0", lambda: self.s("POST", "/wda/tap/0", {"x": x, "y": y})),
            ("actions", lambda: self._actions([(x, y, 0)], hold_ms=40)),
        ])

    async def long_press(self, fx: float, fy: float, ms: int):
        x, y = await self.pt(fx, fy)
        ms = min(max(ms, 500), 3000)
        return await self._first_ok("hold", [
            ("touchAndHold", lambda: self.s("POST", "/wda/touchAndHold",
                                             {"x": x, "y": y, "duration": ms / 1000})),
            ("actions", lambda: self._actions([(x, y, 0)], hold_ms=ms)),
        ])

    async def swipe(self, path: list[list[float]]):
        """path: [[fx, fy, t_ms], ...] tal como lo dibujó el usuario (t relativo).

        El gesto se COMPRIME en el tiempo: se conserva la forma, pero dura como mucho
        `swipe_max_ms`. Si no, un arrastre de 600 ms tardaba 600 ms en ejecutarse
        DESPUÉS de soltar el mouse. Si el usuario frenó antes de soltar (quería dejar
        la lista quieta, no lanzarla), se agrega una pausa corta al final para que iOS
        no aplique inercia.
        """
        if len(path) < 2:
            return
        pts, stop_ms = _shape(path, self.swipe_max_ms)
        conv = []
        for fx, fy, t in pts:
            x, y = await self.pt(fx, fy)
            conv.append((x, y, t))
        (x1, y1, _), (x2, y2, t2) = conv[0], conv[-1]
        variants = {
            "actions": lambda: self._actions(conv, hold_ms=0, end_hold_ms=stop_ms),
            # WDA de Appium: arrastre con velocidad en puntos/seg, sin trayectoria.
            "velocity": lambda: self.s("POST", "/wda/pressAndDragWithVelocity", {
                "fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "pressDuration": 0,
                "holdDuration": stop_ms / 1000,
                "velocity": max(200, math.hypot(x2 - x1, y2 - y1) / max(t2, 1) * 1000)}),
            # Lo que usaba iOS-remote original: lento y siempre a la misma velocidad.
            "drag": lambda: self.s("POST", "/wda/dragfromtoforduration", {
                "fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": 0}),
        }
        if self.swipe_mode in variants:
            return await variants[self.swipe_mode]()
        return await self._first_ok("swipe", list(variants.items()))

    async def _actions(self, pts: list[tuple[int, int, int]], hold_ms: int, end_hold_ms: int = 0):
        x0, y0, t0 = pts[0]
        seq = [{"type": "pointerMove", "duration": 0, "x": x0, "y": y0},
               {"type": "pointerDown", "button": 0}]
        if hold_ms:
            seq.append({"type": "pause", "duration": hold_ms})
        prev = t0
        for x, y, t in pts[1:]:
            seq.append({"type": "pointerMove", "duration": max(t - prev, 10), "x": x, "y": y})
            prev = t
        if end_hold_ms:
            seq.append({"type": "pause", "duration": end_hold_ms})
        seq.append({"type": "pointerUp", "button": 0})
        return await self.s("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger1",
            "parameters": {"pointerType": "touch"}, "actions": seq}]})

    # ---------- teclado / botones ----------
    async def keys(self, text: str):
        return await self.s("POST", "/wda/keys", {"value": list(text)})

    async def home(self):
        return await self._first_ok("home", [
            ("global", lambda: self._req("POST", "/wda/homescreen")),
            ("pressButton", lambda: self.s("POST", "/wda/pressButton", {"name": "home"})),
            ("session", lambda: self.s("POST", "/wda/homescreen")),
        ])

    async def app_switcher(self):
        """Doble Home → selector de apps (iPhone con botón Home: 8, SE…).

        iOS sólo lo toma como DOBLE pulsación si las dos llegan dentro de ~0.3 s.
        1º intento: eventos HID crudos del botón Home (página 0x0C, uso 0x40). Son de más bajo
           nivel que 'pressButton': no esperan a que aparezca la pantalla de inicio entre una
           pulsación y la otra (pressButton sí, y por eso salía un Home normal).
        2º intento (WDA viejo sin esa ruta): dos 'pressButton home' mandados a la vez.
        """
        sid = await self.ensure_session()
        hid = {"page": 0x0C, "usage": 0x40, "duration": 0.05}

        async def hid_press():
            return await self._req("POST", f"/session/{sid}/wda/performIoHidEvent", hid)

        async def btn_press():
            return await self._req("POST", f"/session/{sid}/wda/pressButton", {"name": "home"})

        async def twice(fn):
            res = await asyncio.gather(fn(), fn(), return_exceptions=True)
            errs = [r for r in res if isinstance(r, Exception)]
            if errs:
                raise errs[0]

        return await self._first_ok("switcher", [
            ("hid", lambda: twice(hid_press)),
            ("pressButton", lambda: twice(btn_press)),
        ])

    async def button(self, name: str):  # volumeUp | volumeDown | home
        return await self.s("POST", "/wda/pressButton", {"name": name})

    async def lock(self):
        return await self.s("POST", "/wda/lock")

    async def unlock(self):
        return await self.s("POST", "/wda/unlock")

    async def locked(self) -> bool:
        return bool(await self.s("GET", "/wda/locked"))

    async def rotate(self):
        cur = await self.s("GET", "/orientation")
        new = "LANDSCAPE" if cur == "PORTRAIT" else "PORTRAIT"
        await self.s("POST", "/orientation", {"orientation": new})
        self.size = None  # cambia el ancho/alto en puntos
        return new

    async def activate(self, bundle_id: str):
        """Trae la app al frente SIN reiniciarla (como tocarla en el selector de apps)."""
        return await self.s("POST", "/wda/apps/activate", {"bundleId": bundle_id})

    async def terminate(self, bundle_id: str):
        """Cierra la app (como deslizarla hacia arriba en el selector)."""
        return await self.s("POST", "/wda/apps/terminate", {"bundleId": bundle_id})

    async def launch(self, bundle_id: str):
        return await self.s("POST", "/wda/apps/launch", {"bundleId": bundle_id})

    async def screenshot_png_b64(self) -> str:
        return await self._req("GET", "/screenshot")

    async def stream_settings(self, fps: int, scale: int, quality: int):
        return await self.s("POST", "/appium/settings", {"settings": {
            "mjpegServerFramerate": fps,
            "mjpegScalingFactor": scale,
            "mjpegServerScreenshotQuality": quality}})

    async def close(self):
        await self.http.aclose()


def _shape(path: list[list[float]], max_ms: int, max_pts: int = 4):
    # 4 puntos (inicio, 2 intermedios, fin) conservan la curva del gesto. Medido en un
    # iPhone 8: cada punto extra suma tiempo en WDA (6 puntos ≈1.75 s, 2 puntos ≈1.39 s).
    """Comprime el gesto a <= max_ms y <= max_pts puntos. Devuelve (puntos, pausa_final_ms)."""
    t_end = path[-1][2]
    # ¿frenó antes de soltar? movimiento en los últimos 100 ms < 1.5% de pantalla
    # punto de referencia: el último registrado ANTES de los últimos 100 ms
    ref = next((p for p in reversed(path) if p[2] <= t_end - 100), path[0])
    moved_tail = math.hypot(path[-1][0] - ref[0], path[-1][1] - ref[1])
    stop_ms = 100 if (t_end >= 150 and moved_tail < 0.015) else 0
    # recorta el tramo quieto final (si no, se gasta tiempo moviendo 0 px)
    if stop_ms:
        while len(path) > 2 and math.hypot(path[-2][0] - path[-1][0], path[-2][1] - path[-1][1]) < 0.003:
            path = path[:-1]
    total = max(path[-1][2] - path[0][2], 1)
    k = min(1.0, max_ms / total)
    if len(path) > max_pts:
        idx = [round(i * (len(path) - 1) / (max_pts - 1)) for i in range(max_pts)]
        path = [path[i] for i in idx]
    out = [[fx, fy, round((t - path[0][2]) * k)] for fx, fy, t in path]
    return out, stop_ms

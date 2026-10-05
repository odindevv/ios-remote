"""Diagnóstico de velocidad de WDA en TU teléfono.

    python tests/bench.py            # WDA en 127.0.0.1:8100 (server.py corriendo o relay a mano)
    python tests/bench.py 8101       # otro teléfono
    python tests/bench.py 8100 --rapido   # sólo toques + drag (no prueba los swipes que cuelgan WDA)
    python tests/bench.py 8100 --swipes   # sólo los swipes, para elegir swipe_mode

⚠ Cerrá la pestaña del panel mientras corre: si el panel manda comandos a la vez, los dos
  compiten por el mismo WDA y los tiempos salen inflados.

Es inofensivo: abre Ajustes, toca la barra de estado y hace scroll arriba/abajo.
Mide con los ajustes de fábrica de WDA y con los de ios-remote (sin esperas), y prueba
cada forma de swipe por separado. Al final devuelve los ajustes de WDA como estaban.
"""
import asyncio
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wda_client import WDA  # noqa: E402

TIMEOUT = 30  # s por comando: si WDA se cuelga, se ve cuánto, en vez de cortar a los 15


async def timed(name, fn, n):
    ms, errs = [], []
    for _ in range(n):
        t = time.perf_counter()
        try:
            await fn()
            ms.append((time.perf_counter() - t) * 1000)
        except Exception as e:
            dt = (time.perf_counter() - t) * 1000
            errs.append(f"{type(e).__name__} {str(e)[:50]} ({dt:.0f} ms)")
            if isinstance(e, httpx.TimeoutException) or "404" in str(e):
                break  # no tiene sentido insistir: colgado o no existe
        await asyncio.sleep(0.6)  # deja terminar la inercia del scroll
    if ms:
        print(f"  {name:38} mediana {statistics.median(ms):6.0f} ms   mín {min(ms):5.0f}   máx {max(ms):5.0f}"
              + (f"   ({len(errs)} fallos)" if errs else ""))
    else:
        print(f"  {name:38} FALLA: {errs[0] if errs else '?'}")
    return statistics.median(ms) if ms else None


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rapido = "--rapido" in sys.argv
    solo_swipes = "--swipes" in sys.argv
    port = args[0] if args else "8100"
    w = WDA(f"http://127.0.0.1:{port}", timeout=TIMEOUT)
    st = await w.status()
    b = (st or {}).get("build", {}) or {}
    ios = ((st or {}).get("os", {}) or {}).get("version", "?")
    print(f"\nWDA {b.get('version', '?')} · iOS {ios} · puerto {port}")

    sid = await w.ensure_session()
    try:
        cur = await w._req("GET", f"/session/{sid}/appium/settings") or {}
        print("ajustes actuales:", {k: cur.get(k) for k in ("waitForIdleTimeout", "animationCoolOffTimeout")})
    except Exception as e:
        print("este WDA no expone /appium/settings:", str(e)[:60])

    await w.launch("com.apple.Preferences")
    await asyncio.sleep(1.5)
    await w.window_size()

    flip = {"up": True}

    def scroll():
        flip["up"] = not flip["up"]
        return (0.75, 0.35) if flip["up"] else (0.35, 0.75)

    async def actions_simple():  # lo mínimo que existe en W3C: bajar, mover 200 ms, soltar
        a, b_ = scroll()
        x, y1 = await w.pt(0.5, a)
        _, y2 = await w.pt(0.5, b_)
        await w.s("POST", "/actions", {"actions": [{"type": "pointer", "id": "f1",
            "parameters": {"pointerType": "touch"}, "actions": [
                {"type": "pointerMove", "duration": 0, "x": x, "y": y1},
                {"type": "pointerDown", "button": 0},
                {"type": "pointerMove", "duration": 200, "x": x, "y": y2},
                {"type": "pointerUp", "button": 0}]}]})

    async def swipe_mode(mode):
        a, b_ = scroll()
        w.swipe_mode = mode
        await w.swipe([[0.5, a, 0], [0.5, (a + b_) / 2, 90], [0.5, b_, 180]])

    async def tap():
        await w.tap(0.5, 0.01)

    if not solo_swipes:
        print("\n1) Con los ajustes de FÁBRICA de WDA")
        await timed("tap", tap, 5)

    print("\n2) Con los ajustes de ios-remote (sin esperas)")
    await w.speed_on()
    try:
        now = await w._req("GET", f"/session/{await w.ensure_session()}/appium/settings") or {}
        print("  ajustes aplicados:", {k: now.get(k) for k in ("waitForIdleTimeout", "animationCoolOffTimeout")})
    except Exception as e:
        print("  no pude leer los ajustes:", str(e)[:60])
    try:
        if not solo_swipes:
            await timed("tap", tap, 8)
        res = {}
        res["drag"] = await timed("swipe drag (el del original)", lambda: swipe_mode("drag"), 4)
        if rapido:
            return
        res["actions"] = await timed("swipe actions (trayectoria)", lambda: swipe_mode("actions"), 4)
        await timed("swipe actions mínimo (diagnóstico)", actions_simple, 3)
        res["velocity"] = await timed("swipe velocity", lambda: swipe_mode("velocity"), 4)
    finally:
        await w.speed_off()
        print("\najustes de WDA devueltos a como estaban")
        if rapido:
            await w.close()

    ok = {k: v for k, v in res.items() if v is not None}
    if ok:
        best = min(ok, key=ok.get)
        print(f'\n→ Swipe más rápido que funciona: "{best}". En la config: "swipe_mode": "{best}"')
    else:
        print("\n→ Ningún swipe funcionó: pegale esta salida a Claude.")
    await w.close()


try:
    asyncio.run(main())
except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
    sys.exit(f"\nWDA no contesta ({type(e).__name__}). El relay está pero WDA del otro lado no:\n"
             "  · ¿sigue abierta la ventana de 'tidevice xctest'? Si se cerró o dice error, relanzala.\n"
             "  · ¿'python -m tidevice list' muestra el iPhone? Si no, se cortó el USB.\n"
             "  · Comprobá con:  curl http://127.0.0.1:8100/status")

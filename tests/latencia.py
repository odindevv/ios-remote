"""¿Cuándo se VE el toque y cuándo CONTESTA WDA? Las dos cosas por separado.

    python tests/latencia.py                              # usa config-iphone-propio.json
    python tests/latencia.py --config config.json --id B  # otro teléfono
    python tests/latencia.py --fps 5                      # video más lento (menos carga para WDA)

Con WDA corriendo (server.py o tidevice a mano) y la pestaña del panel CERRADA.
Abre Ajustes, toca una fila de la lista y mira el video: el momento en que la fila se
ilumina o cambia la pantalla es cuando el toque llegó de verdad al teléfono.
"""
import asyncio
import io
import json
import statistics
import sys
import time

import httpx
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mjpeg import MjpegHub  # noqa: E402
from wda_client import WDA  # noqa: E402

try:
    from PIL import Image, ImageChops, ImageStat  # viene con tidevice
except ImportError:
    sys.exit("Falta Pillow:  python -m pip install pillow")

N = 6


def arg(name, default):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def small(jpg: bytes):
    return Image.open(io.BytesIO(jpg)).convert("L").resize((60, 120))


def differs(a, b) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


async def main():
    cfg = json.loads((ROOT / arg("--config", "config-iphone-propio.json")).read_text(encoding="utf-8"))
    devs = cfg["devices"]
    dev = next((d for d in devs if d["id"] == arg("--id", devs[0]["id"])), devs[0])
    w = WDA(f"http://127.0.0.1:{dev['wda_port']}", timeout=30)
    hub = MjpegHub("127.0.0.1", dev.get("mjpeg_port"), udid=dev.get("udid") or None)

    await w.ensure_session()
    fps = int(arg("--fps", "30"))
    await w.stream_settings(fps, int(arg("--scale", "50")), 40)
    await w.speed_on()
    frames: list[tuple[float, bytes]] = []

    async def collect():
        seq = 0
        while True:
            seq, f = await hub.next_frame(seq)
            if f:
                frames.append((time.perf_counter(), f))
                del frames[:-90]

    hub.acquire()
    col = asyncio.create_task(collect())
    try:
        t_wait = time.perf_counter()
        while not frames:
            if time.perf_counter() - t_wait > 15:
                sys.exit("No llega video. ¿El udid de la config es el de este teléfono?")
            await asyncio.sleep(0.1)
        print(f"\nvideo OK a {fps} fps · teléfono {dev['id']} · midiendo {N} toques en Ajustes\n")
        print(f"  {'#':>2}  {'se VE en pantalla':>18}  {'WDA contesta':>13}")
        vis, resp, pares = [], [], []
        for i in range(N):
            await w.launch("com.apple.Preferences")
            await asyncio.sleep(2.5)                     # que la pantalla quede quieta
            base = small(frames[-1][1])
            t0 = time.perf_counter()
            task = asyncio.create_task(w.tap(0.5, 0.45))  # una fila de la lista
            t_vis = None
            while time.perf_counter() - t0 < 8:
                await asyncio.sleep(0.01)
                for t, f in reversed(frames):
                    if t <= t0:
                        break
                    if differs(small(f), base) > 2.0:
                        t_vis = t
                if t_vis:
                    break
            await task
            t_resp = time.perf_counter()
            v = (t_vis - t0) * 1000 if t_vis else None
            r = (t_resp - t0) * 1000
            resp.append(r)
            if v is not None:
                vis.append(v)
                pares.append(r - v)
            print(f"  {i + 1:>2}  {('%.0f ms' % v) if v is not None else 'no cambió':>18}  {r:>10.0f} ms")
        print()
        sin = N - len(vis)
        if sin:
            print(f"  ⚠ {sin} de {N} toques no cambiaron la pantalla: si coincide con 'stream cerrado' o")
            print("    'not ready', se cortó el USB; si no, el iPhone se bloqueó (Auto-Lock) o el toque no llegó.")
        if vis:
            # Se compara FILA POR FILA (sólo las que sí cambiaron): mezclar medianas de filas
            # distintas da conclusiones falsas cuando hay toques fallidos de 8 s.
            gap = statistics.median(pares)
            print(f"  en los toques que se vieron: se ve a los {statistics.median(vis):.0f} ms,"
                  f" WDA contesta {gap:.0f} ms después")
            if gap > 500:
                print("  → El toque llega rápido y WDA tarda en CONFIRMAR. El panel puede dejar de esperar")
                print("    esa confirmación: se sentiría tan rápido como la primera columna.")
            else:
                print("  → El toque tarda de verdad en llegar al teléfono. No se arregla sin esperar la")
                print("    respuesta: el retraso está en cómo se lanzó WDA o en el build de WDA.")
        else:
            print("  La pantalla no cambió con el toque: probá otra posición o mirá el teléfono.")
    finally:
        col.cancel()
        hub.release()
        await w.speed_off()
        await w.close()


try:
    asyncio.run(main())
except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
    sys.exit(f"\nWDA no contesta ({type(e).__name__}). El relay está pero WDA del otro lado no:\n"
             "  · ¿sigue abierta la ventana de 'tidevice xctest'? Si se cerró o dice error, relanzala.\n"
             "  · ¿'python -m tidevice list' muestra el iPhone? Si no, se cortó el USB.\n"
             "  · Comprobá con:  curl http://127.0.0.1:8100/status")

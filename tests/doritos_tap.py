"""Prueba qué forma de tocar funciona con una app SIN árbol de accesibilidad (Doritos).

Uso (con el panel corriendo y el teléfono con Start):
  1. En el iPhone, abre Doritos y déjalo al frente.
  2. python tests\\doritos_tap.py <puerto>      (el puerto sale en la ventana negra:
                                                "Start: ... → WDA en el puerto 8200")
Toca un punto vacío del fondo (esquina izquierda, a media altura) varias veces, mide cuánto
tarda cada forma y al final deja los ajustes de WDA como estaban.
Córrelo cuando la automatización NO esté en medio de un ciclo.
"""
import sys
import time

import httpx

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
X, Y = 12, 420           # fondo, fuera de la tarjeta de la licencia
c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False)

st = c.get("/status").json()
sid = st.get("sessionId") or c.post("/session", json={"capabilities": {"alwaysMatch": {}}}).json()["sessionId"]
orig = c.get(f"/session/{sid}/appium/settings").json().get("value", {})
keep = {k: orig[k] for k in ("customSnapshotTimeout", "defaultActiveApplication",
                             "snapshotTimeout", "waitForIdleTimeout", "animationCoolOffTimeout")
        if k in orig}
print("ajustes actuales:", keep)


def timed(label, fn):
    t0 = time.perf_counter()
    try:
        r = fn()
        ok = r.status_code == 200 and not (r.json().get("value") or {}).get("error")
        msg = "ok" if ok else str(r.json().get("value"))[:120]
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
    print(f"  {label:<44} {(time.perf_counter() - t0) * 1000:7.0f} ms  {msg}")


def tap():
    return c.post(f"/session/{sid}/wda/tap", json={"x": X, "y": Y})


def tap_actions():
    return c.post(f"/session/{sid}/actions", json={"actions": [{
        "type": "pointer", "id": "f", "parameters": {"pointerType": "touch"},
        "actions": [{"type": "pointerMove", "duration": 0, "x": X, "y": Y},
                    {"type": "pointerDown"}, {"type": "pause", "duration": 50},
                    {"type": "pointerUp"}]}]})


def setting(**kw):
    return c.post(f"/session/{sid}/appium/settings", json={"settings": kw})


try:
    print("\nA) como hoy")
    timed("activeAppInfo", lambda: c.get("/wda/activeAppInfo"))
    for i in range(2):
        timed(f"wda/tap #{i + 1}", tap)
        timed(f"actions #{i + 1}", tap_actions)

    print("\nB) waitForIdle/animation en 0")
    setting(waitForIdleTimeout=0, animationCoolOffTimeout=0)
    for i in range(2):
        timed(f"wda/tap #{i + 1}", tap)
        timed(f"actions #{i + 1}", tap_actions)

    if "customSnapshotTimeout" in orig:
        print("\nC) + customSnapshotTimeout 0.5 s (corta la espera de accesibilidad)")
        setting(customSnapshotTimeout=0.5)
        timed("activeAppInfo", lambda: c.get("/wda/activeAppInfo"))
        for i in range(2):
            timed(f"wda/tap #{i + 1}", tap)
            timed(f"actions #{i + 1}", tap_actions)

    print("\nD) + defaultActiveApplication = Doritos")
    setting(defaultActiveApplication="com.secretsauce.doritosapp")
    for i in range(2):
        timed(f"wda/tap #{i + 1}", tap)
        timed(f"actions #{i + 1}", tap_actions)
finally:
    if keep:
        setting(**keep)
        print("\najustes restaurados:", keep)

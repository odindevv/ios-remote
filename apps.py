"""Apps abiertas en el teléfono, para el selector de apps del panel (⧉).

WDA no sabe qué apps están corriendo; el servicio de instruments del teléfono sí (el mismo
dato que usa Xcode). Se cruzan esos procesos con la lista de apps instaladas.

  · iOS 16 o anterior → tidevice (rápido).
  · iOS 17 o posterior → pymobiledevice3 con túnel '--userspace' (tarda unos segundos:
    arma el túnel en cada consulta). tidevice no sabe hablar con iOS 17+.
Es bloqueante: el servidor lo llama desde un hilo.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

_installed: dict[str, tuple[float, list]] = {}   # udid -> (cuándo, apps instaladas)
_HIDE = ("com.apple.springboard", "WebDriverAgentRunner", "com.apple.PosterBoard",
         "com.apple.InCallService", "com.apple.Spotlight", "com.apple.CarPlayTemplateUIHost")


def _installed_apps(udid: str | None) -> list:
    import tidevice
    key = udid or "-"
    cached = _installed.get(key)
    if not cached or time.time() - cached[0] > 300:   # la lista de instaladas cambia poco
        cached = (time.time(), list(tidevice.Device(udid or None).installation.iter_installed(app_type=None)))
        _installed[key] = cached
    return cached[1]


def _norm(p: str) -> str:
    return p[len("/private"):] if p.startswith("/private") else p


def _procs_pmd(udid: str | None) -> list[dict]:
    """Procesos vía pymobiledevice3 (iOS 17+)."""
    cmd = [sys.executable, "-m", "pymobiledevice3", "developer", "dvt", "proclist", "--userspace"]
    if udid:
        cmd += ["--udid", udid]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NO_COLOR="1")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    r = subprocess.run(cmd, capture_output=True, timeout=45, env=env, creationflags=flags)
    out = r.stdout.decode("utf-8", "replace")
    i = out.find("[")
    if i < 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else "pymobiledevice3 proclist returned nothing")
    procs, _ = json.JSONDecoder().raw_decode(out[i:])
    # mismo formato que tidevice: se agrega bundle_id / display_name cruzando con las instaladas
    by_exe = {}
    for info in _installed_apps(udid):
        if info.get("Path") and info.get("CFBundleExecutable"):
            by_exe[_norm(info["Path"] + "/" + info["CFBundleExecutable"])] = info
    for p in procs:
        info = by_exe.get(_norm(p.get("realAppName") or ""), {})
        p["bundle_id"] = info.get("CFBundleIdentifier", "")
        p["display_name"] = info.get("CFBundleDisplayName") or info.get("CFBundleName") or p.get("name")
    return procs


def _procs_tidevice(udid: str | None) -> list[dict]:
    import tidevice
    infos = _installed_apps(udid)
    with tidevice.Device(udid or None).connect_instruments() as ts:
        return list(ts.app_process_list(infos))


def running_apps(udid: str | None, ios_major: int = 0) -> list[dict]:
    procs = _procs_pmd(udid) if ios_major >= 17 else _procs_tidevice(udid)
    by_bundle = {i.get("CFBundleIdentifier"): i for i in _installed_apps(udid)}
    out, seen = [], set()
    for p in sorted(procs, key=lambda p: -int(p.get("pid") or 0)):   # pid alto = abierta hace menos
        b = p.get("bundle_id") or ""
        if not p.get("isApplication") or not b or b in seen or any(h in b for h in _HIDE):
            continue
        if "hidden" in (by_bundle.get(b, {}).get("SBAppTags") or []):
            continue
        seen.add(b)
        out.append({"bundle": b, "name": p.get("display_name") or p.get("name") or b,
                    "pid": p.get("pid")})
    return out

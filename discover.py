"""Detección automática de iPhones conectados por USB.

Para cada iPhone enchufado averigua: nombre, modelo, versión de iOS y si tiene WebDriverAgent
instalado (y con qué bundle ID). Usa los servicios normales del teléfono (usbmux + lockdown +
lista de apps), que funcionan en cualquier versión de iOS sin modo desarrollador.
Es bloqueante: el servidor lo llama desde un hilo.
"""
from __future__ import annotations

import time

_cache: dict[str, dict] = {}        # udid -> info (nombre, iOS, bundle de WDA…)
_WDA_RECHECK_S = 30                 # si no tenía WDA, se vuelve a mirar cada 30 s


def _wda_bundle(dev) -> str | None:
    """El bundle ID del WebDriverAgent instalado (Sideloadly le agrega el team al final)."""
    for app in dev.installation.iter_installed(app_type="User"):
        b = app.get("CFBundleIdentifier", "")
        if "WebDriverAgentRunner" in b and "xctrunner" in b:
            return b
    return None


def scan() -> list[dict]:
    """Lista de iPhones conectados por USB, con su info. Nunca tira: si un teléfono no
    contesta (bloqueado, sin 'Confiar'…) vuelve igual, con el motivo en 'problem'."""
    import tidevice
    try:
        listed = [d for d in tidevice.Usbmux().device_list()
                  if str(getattr(d.conn_type, "value", d.conn_type)).lower() == "usb"]
    except Exception as e:
        return [{"udid": None, "problem": f"Can't talk to the Apple USB service: {e}"}]

    out = []
    now = time.time()
    for d in listed:
        udid = d.udid
        info = _cache.get(udid)
        if info is None or info.get("problem"):
            info = {"udid": udid, "name": udid[-6:], "model": "", "ios": None,
                    "bundle": None, "problem": None, "checked": 0}
            try:
                dev = tidevice.Device(udid)
                v = dev.device_info() or {}
                info["name"] = v.get("DeviceName") or info["name"]
                info["model"] = v.get("ProductType", "")
                info["ios"] = v.get("ProductVersion")
            except Exception:
                info["problem"] = "Locked or not trusted: unlock it and tap Trust"
                out.append(info)
                continue
            _cache[udid] = info
        if not info.get("bundle") and now - info.get("checked", 0) > _WDA_RECHECK_S:
            info["checked"] = now
            try:
                info["bundle"] = _wda_bundle(tidevice.Device(udid))
            except Exception:
                pass
        out.append(dict(info))
    return out


def ios_major(version: str | None) -> int:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return 0

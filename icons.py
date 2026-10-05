"""Íconos reales de las apps, guardados en disco (cache/icons/<bundle>.png).

En cuanto se detecta un iPhone, prefetch() baja en segundo plano los íconos de TODAS las apps
de su pantalla de inicio, así el selector de apps (⧉) los muestra al instante. Usa el servicio
SpringBoard del teléfono directamente (una sola conexión para todos los íconos), que funciona
en cualquier versión de iOS sin modo desarrollador.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

log = logging.getLogger("icons")
ICON_DIR = Path(__file__).parent / "cache" / "icons"
_lock: asyncio.Lock | None = None   # un teléfono a la vez: el 2º reusa lo que bajó el 1º
_done: set[str] = set()          # teléfonos ya precargados en esta corrida


async def _maybe(x):
    """pymobiledevice3 pasó de API normal a async entre versiones: sirve para las dos."""
    return await x if inspect.isawaitable(x) else x


async def _springboard(udid: str | None):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.springboard import SpringBoardServicesService
    ld = await _maybe(create_using_usbmux(serial=udid, autopair=False))
    return ld, SpringBoardServicesService(lockdown=ld)


async def _close(ld):
    try:
        await _maybe(ld.close())
    except Exception:
        pass


def _bundles(state) -> list[str]:
    """Todos los bundle IDs de la pantalla de inicio (páginas, carpetas y dock)."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            b = x.get("bundleIdentifier")
            if b:
                out.append(b)
            for page in x.get("iconLists", []):
                walk(page)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(state)
    return list(dict.fromkeys(out))


def path_for(bundle: str) -> Path:
    return ICON_DIR / f"{bundle}.png"


async def _save(sb, bundle: str) -> bool:
    png = await _maybe(sb.get_icon_pngdata(bundle))
    if not png:
        return False
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path_for(bundle).with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.replace(path_for(bundle))
    return True


async def fetch_one(udid: str | None, bundle: str) -> bool:
    """Un ícono puntual (el que pide el selector si todavía no estaba guardado)."""
    if path_for(bundle).exists():
        return True
    ld, sb = await _springboard(udid)
    try:
        return await _save(sb, bundle)
    finally:
        await _close(ld)


async def prefetch(udid: str, name: str = ""):
    """Guarda en disco los íconos que falten de todas las apps del teléfono. Una vez por corrida."""
    if udid in _done:
        return
    global _lock
    _lock = _lock or asyncio.Lock()
    async with _lock:
        if udid in _done:
            return
        try:
            ld, sb = await _springboard(udid)
        except Exception as e:
            log.info("íconos de %s: todavía no (%s)", name or udid, e)
            return
        new = 0
        try:
            missing = [b for b in _bundles(await _maybe(sb.get_icon_state())) if not path_for(b).exists()]
            for b in missing:
                try:
                    new += await _save(sb, b)
                except Exception:
                    pass
            _done.add(udid)
            if new:
                log.info("íconos de %s: %d nuevos guardados (cache/icons)", name or udid, new)
        except Exception as e:
            log.info("íconos de %s: no se pudo leer la pantalla de inicio (%s)", name or udid, e)
        finally:
            await _close(ld)

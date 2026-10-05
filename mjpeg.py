"""Lector del MJPEG de WDA (puerto 9100 del teléfono): directo por USB o por un relay.

Diferencia clave con iOS-remote original: allá el <img> del navegador se conectaba
DIRECTO a 127.0.0.1:8200, así que sólo funcionaba en la misma PC. Acá el servidor abre
UNA conexión al MJPEG y reparte el último frame a N visores por WebSocket. Si un visor
es lento, se salta frames (se queda siempre con el más nuevo) en vez de acumular lag.
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("mjpeg")
_CL = re.compile(rb"content-length:\s*(\d+)", re.I)


def _usb_socket(udid: str | None, device_port: int):
    """Abre una conexión TCP directa al puerto `device_port` DEL TELÉFONO a través de
    usbmux (lo mismo que hace `tidevice relay`, pero sin abrir ningún puerto en la PC).
    Es bloqueante: se llama desde un hilo. Devuelve (socket, objeto_a_mantener_vivo)."""
    import tidevice  # import tardío: sólo hace falta en modo USB
    d = tidevice.Device(udid or None)
    conn = d.create_inner_connection(device_port)
    return conn.get_socket(), conn


def in_daemon_thread(fn, *args):
    """Como asyncio.to_thread, pero en un hilo daemon: si la llamada bloqueante (tidevice)
    se cuelga, no ocupa el pool de hilos para siempre ni impide cerrar el servidor."""
    import threading
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def run():
        try:
            r = fn(*args)
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(r))
        except BaseException as e:  # noqa: BLE001
            loop.call_soon_threadsafe(lambda e=e: fut.done() or fut.set_exception(e))
    threading.Thread(target=run, daemon=True).start()
    return fut


class MjpegHub:
    def __init__(self, host: str, port: int | None, udid: str | None = None,
                 device_port: int = 9100):
        """port=None → video directo por USB (udid); port=N → por un relay en 127.0.0.1:N."""
        self.host, self.port = host, port
        self.udid, self.device_port = udid, device_port
        self.frame: bytes | None = None
        self.seq = 0
        self._cond = asyncio.Condition()
        self._task: asyncio.Task | None = None
        self.viewers = 0
        self.fps = 0.0

    # --- ciclo de vida: sólo se lee el stream si hay alguien mirando ---
    def acquire(self):
        self.viewers += 1
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    def release(self):
        self.viewers = max(0, self.viewers - 1)
        if self.viewers == 0 and self._task:
            self._task.cancel()
            self._task = None

    async def next_frame(self, last_seq: int, timeout: float = 5.0) -> tuple[int, bytes | None]:
        async with self._cond:
            if self.seq == last_seq:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except asyncio.TimeoutError:
                    return last_seq, None
            return self.seq, self.frame

    async def _publish(self, jpg: bytes):
        async with self._cond:
            self.frame = jpg
            self.seq += 1
            self._cond.notify_all()

    async def _run(self):
        backoff = 1.0
        while True:
            try:
                await self._read_stream()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # relay caído, WDA reiniciando, etc.
                log.warning("mjpeg %s:%s: %s (reintento en %.0fs)", self.host, self.port, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)

    async def _read_stream(self):
        keep = None
        if self.port:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), 5)
        else:
            sock, keep = await asyncio.wait_for(
                in_daemon_thread(_usb_socket, self.udid, self.device_port), 10)
            reader, writer = await asyncio.open_connection(sock=sock)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            buf = b""
            loop = asyncio.get_running_loop()
            t0, n = loop.time(), 0
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), 10)
                if not chunk:
                    raise ConnectionError("stream cerrado")
                buf += chunk
                # Parseo por marcadores JPEG (SOI FFD8 … EOI FFD9). Usa Content-Length
                # cuando está, que es más seguro que buscar FFD9.
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        buf = buf[-1:]
                        break
                    m = None
                    for m in _CL.finditer(buf, 0, soi):
                        pass
                    if m:
                        end = soi + int(m.group(1))
                        if len(buf) < end:
                            break
                        jpg = buf[soi:end]
                    else:
                        eoi = buf.find(b"\xff\xd9", soi + 2)
                        if eoi < 0:
                            break
                        end = eoi + 2
                        jpg = buf[soi:end]
                    buf = buf[end:]
                    await self._publish(jpg)
                    n += 1
                    now = loop.time()
                    if now - t0 >= 2:
                        self.fps = n / (now - t0)
                        t0, n = now, 0
                if len(buf) > 8_000_000:
                    buf = b""
        finally:
            writer.close()
            if keep is not None:
                try:
                    keep.close()
                except Exception:
                    pass

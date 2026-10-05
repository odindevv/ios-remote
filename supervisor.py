"""Watchdog del lanzador de WDA (pymobiledevice3 o tidevice): mantiene vivos el test
runner (WDA) y su reenvío de puerto, uno por teléfono.

Si alguno se cae (teléfono desconectado, WDA reiniciado), lo vuelve a levantar con
espera creciente. Hilos + subprocess.Popen a propósito: así no depende del tipo de
event loop de asyncio en Windows.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("supervisor")
LOGDIR = Path(__file__).parent / "logs"


_ALL: list["Proc"] = []


def bind_children_to_this_process():
    """Windows: mete a este proceso en un Job Object con KILL_ON_JOB_CLOSE. Todo lo que
    lance después (tidevice.exe y el python que tidevice.exe lanza adentro) hereda el job,
    y cuando este proceso termina —Ctrl+C, cerrar la ventana o un crash— Windows cierra
    el job y mata a todos los hijos. Sin esto, los relays quedaban vivos ocupando puertos.
    Sólo afecta a procesos lanzados por ESTE servidor: el vigía y sus tidevice no."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.GetCurrentProcess.restype = wintypes.HANDLE

        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = (job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
              and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()))
        if ok:
            globals()["_JOB"] = job   # el handle tiene que vivir lo mismo que el proceso
            log.info("procesos hijos atados a este servidor (se cierran con él)")
        else:
            log.warning("no pude crear el Job Object (error %s); al cerrar se usa taskkill",
                        ctypes.get_last_error())
    except Exception as e:
        log.warning("no pude crear el Job Object: %s; al cerrar se usa taskkill", e)


def stop_all():
    """Cierra los tidevice que lanzó este servidor, con todo su árbol de procesos."""
    for pr in _ALL:
        pr.kill()


def _default_launcher() -> str:
    """pymobiledevice3 si está instalado: con tidevice, en iOS 16 los toques tardaban ~2 s
    o se colgaban 8 s y los swipes /actions no terminaban nunca (medido 2026-10-04)."""
    import importlib.util
    return "pymobiledevice3" if importlib.util.find_spec("pymobiledevice3") else "tidevice"


def ios_major(dev: dict) -> int:
    """Versión principal de iOS del teléfono: de la config/detección, o se le pregunta."""
    v = dev.get("ios")
    if not v and dev.get("udid"):
        try:
            import tidevice
            v = tidevice.Device(dev["udid"]).get_value("ProductVersion")
        except Exception:
            v = None
    try:
        return int(str(v).split(".")[0])
    except (TypeError, ValueError):
        return 0


def _tidevice() -> list[str]:
    exe = shutil.which("tidevice")
    return [exe] if exe else [sys.executable, "-m", "tidevice"]


class Proc:
    def __init__(self, name: str, args: list[str], pre: list[str] | None = None):
        """pre: comando que se corre (y se espera) antes de CADA arranque, p. ej. montar la
        imagen de desarrollador, que se desmonta cada vez que el iPhone se reinicia."""
        self.name, self.args, self.pre = name, args, pre
        self.p: subprocess.Popen | None = None
        self.restarts = 0
        self.stop = threading.Event()

    def start(self):
        _ALL.append(self)
        threading.Thread(target=self._loop, daemon=True, name=self.name).start()

    def _loop(self):
        LOGDIR.mkdir(exist_ok=True)
        backoff = 2
        while not self.stop.is_set():
            with open(LOGDIR / f"{self.name}.log", "ab") as out:
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                if self.pre:
                    try:
                        subprocess.run(self.pre, stdout=out, stderr=subprocess.STDOUT,
                                       creationflags=flags, timeout=180)
                    except Exception as e:
                        log.warning("%s: paso previo falló: %s", self.name, e)
                log.info("arrancando %s: %s", self.name, " ".join(self.args))
                t0 = time.time()
                self.p = subprocess.Popen(self.args, stdout=out, stderr=subprocess.STDOUT,
                                          creationflags=flags)
                self.p.wait()
            if self.stop.is_set():
                break
            self.restarts += 1
            backoff = 2 if time.time() - t0 > 60 else min(backoff * 2, 60)
            log.warning("%s terminó (código %s): %s — reinicio #%d en %ds",
                        self.name, self.p.returncode, self._last_line(), self.restarts, backoff)
            self.stop.wait(backoff)

    def _last_line(self) -> str:
        try:
            lines = (LOGDIR / f"{self.name}.log").read_text(errors="replace").strip().splitlines()
            return lines[-1][-200:] if lines else "(sin salida)"
        except OSError:
            return "(sin log)"

    def kill(self):
        self.stop.set()
        if not self.p or self.p.poll() is not None:
            return
        if sys.platform == "win32":
            # tidevice.exe es un lanzador que abre un python.exe hijo: terminate() mataría
            # sólo al lanzador y el hijo seguiría con el puerto tomado. /T = todo el árbol.
            subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            self.p.terminate()

    @property
    def alive(self) -> bool:
        return bool(self.p and self.p.poll() is None)


def start_for_device(dev: dict, video_only: bool = False) -> list[Proc]:
    """dev: entrada de config.json. Puertos LOCALES distintos por teléfono;
    dentro del teléfono WDA siempre escucha en 8100 (HTTP) y 9100 (MJPEG).

    video_only ("manage_tidevice": "video"): otro programa ya mantiene vivos el xctest y el
    relay de WDA; acá sólo se levanta el relay del video, que es lo único que falta."""
    td = _tidevice()
    udid = ["--udid", dev["udid"]] if dev.get("udid") else []
    slug = dev["id"]
    launcher = dev.get("launcher") or _default_launcher()
    major = ios_major(dev)
    if major >= 17 and launcher != "pymobiledevice3":
        log.error("teléfono %s: iOS %s necesita pymobiledevice3 (corré SETUP.bat); tidevice no sirve en iOS 17+",
                  slug, major)
        return []
    if launcher == "pymobiledevice3":
        pmd = [sys.executable, "-m", "pymobiledevice3"]
        procs = [
            # DYLD_INSERT_LIBRARIES vacío: pymobiledevice3 inyecta libMainThreadChecker.dylib,
            # que no existe en iOS moderno. Sin jailbreak iOS lo ignora; CON jailbreak
            # (palera1n/Dopamine) mata el proceso al arrancar (error 2). Vaciarlo sirve para
            # los dos casos.
            # iOS 17+: Apple exige un túnel para las herramientas de desarrollo. '--userspace'
            # lo arma dentro de pymobiledevice3 mismo: sin administrador ni drivers.
            # (Probado en iPhone 14 Pro Max, iOS 18.7.8, 2026-10-05.)
            Proc(f"{slug}-xctest",
                 pmd + ["developer", "dvt", "xcuitest", dev["bundle_id"]] + udid
                 + ["--env", "DYLD_INSERT_LIBRARIES="] + (["--userspace"] if major >= 17 else []),
                 pre=pmd + ["mounter", "auto-mount"] + udid),
            Proc(f"{slug}-relay-http",
                 pmd + ["usbmux", "forward", str(dev["wda_port"]), "8100"] + udid),
        ]
    else:
        procs = [
            Proc(f"{slug}-xctest", td + udid + ["xctest", "--bundle-id", dev["bundle_id"]]),
            Proc(f"{slug}-relay-http", td + udid + ["relay", str(dev["wda_port"]), "8100"]),
        ]
    log.info("teléfono %s: WDA se lanza con %s%s", slug, launcher,
             f" (iOS {major}: túnel userspace)" if major >= 17 else "")
    if dev.get("mjpeg_port"):  # sin mjpeg_port el video va directo por USB: no hace falta relay
        procs.append(Proc(f"{slug}-relay-mjpeg",
                          td + udid + ["relay", str(dev["mjpeg_port"]), "9100"]))
    if video_only:
        procs = [p for p in procs if p.name.endswith("relay-mjpeg")]
    for p in procs:
        p.start()
        time.sleep(0.5)
    return procs

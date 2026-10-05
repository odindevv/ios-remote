"""Self-test of iOS Remote — no phone needed.

    python tests\\selftest.py

Starts a fake WDA and the real server on spare ports (it does NOT touch a server that is
already running), checks every feature end to end, and prints PASS / FAIL for each one.
Exit code 0 = everything passed.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WDA_PORT, MJPEG_PORT, WEB_PORT = 18100, 19100, 15000
WEB = f"http://127.0.0.1:{WEB_PORT}"
FAKE = f"http://127.0.0.1:{WDA_PORT}"

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def wait_http(url, secs=20):
    t = time.time()
    while time.time() - t < secs:
        try:
            httpx.get(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


async def ws_session(commands, origin=None):
    """Opens the panel's WebSocket, sends commands, returns (replies, frames)."""
    headers = {"Origin": origin} if origin else {"Origin": WEB}
    replies, frames = {}, 0
    url = f"ws://127.0.0.1:{WEB_PORT}/ws/A"
    try:
        conn = websockets.connect(url, additional_headers=headers)   # websockets >= 14
    except TypeError:
        conn = websockets.connect(url, extra_headers=headers)        # versiones viejas
    async with conn as ws:
        for c in commands:
            await ws.send(json.dumps(c))
        t = time.time()
        while (len(replies) < len(commands) or frames < 5) and time.time() - t < 15:
            m = await asyncio.wait_for(ws.recv(), 10)
            if isinstance(m, bytes):
                frames += m[:2] == b"\xff\xd8"
            else:
                r = json.loads(m)
                replies[r["id"]] = r
    return replies, frames


def main():
    print("\niOS Remote · self-test (fake phone)\n")
    cfg = {"host": "127.0.0.1", "port": WEB_PORT, "token": "", "auto": False,   # no tocar iPhones reales
           "gestures": {"swipe_max_ms": 220, "swipe_mode": "actions"},
           "devices": [{"id": "A", "name": "Test phone", "udid": "", "bundle_id": "x",
                        "wda_port": WDA_PORT, "mjpeg_port": MJPEG_PORT, "manage_tidevice": False,
                        "stream": {"fps": 25, "scale": 50, "quality": 40}}]}
    cfg_file = Path(tempfile.gettempdir()) / "ios-remote-selftest.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    fake = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_wda.py"), str(WDA_PORT), str(MJPEG_PORT)], **quiet)
    server = None
    try:
        if not wait_http(f"{FAKE}/status"):
            print("  FAIL  fake WDA did not start (are ports 18100/19100 busy?)")
            return 1
        server = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--config", str(cfg_file),
                                   "--no-tidevice"], cwd=str(ROOT), **quiet)
        if not wait_http(f"{WEB}/api/devices"):
            print("  FAIL  server did not start (is port 15000 busy?)")
            return 1

        print("Pages")
        r = httpx.get(f"{WEB}/")
        check("phone list page loads", r.status_code == 200 and "Phones" in r.text)
        r = httpx.get(f"{WEB}/control?dev=A")
        check("control page loads", r.status_code == 200 and 'id="screen"' in r.text)

        print("Phone info")
        d = httpx.get(f"{WEB}/api/devices").json()[0]
        check("WDA detected as ready", d["wda"] is True, d)
        check("iOS version and battery read", d["ios"] == "16.7.16" and d["battery"] == 76, d)
        r = httpx.get(f"{WEB}/api/A/thumb")
        check("thumbnail", r.status_code == 200 and r.headers["content-type"].startswith("image/"))
        r = httpx.get(f"{WEB}/api/A/screenshot")
        check("screenshot download", r.status_code == 200 and r.content[:4] == b"\x89PNG")

        print("Live control")
        cmds = [
            {"id": 1, "op": "tap", "x": 0.5, "y": 0.5},
            {"id": 2, "op": "swipe", "path": [[0.5, 0.72, 0], [0.5, 0.30, 160]]},
            {"id": 3, "op": "hold", "x": 0.3, "y": 0.3, "ms": 800},
            {"id": 4, "op": "keys", "text": "hi\n"},
            {"id": 5, "op": "home"},
            {"id": 6, "op": "button", "name": "volumeUp"},
            {"id": 7, "op": "lock"},
            {"id": 8, "op": "launch", "bundle": "com.apple.Preferences"},
            {"id": 9, "op": "activate", "bundle": "com.apple.Preferences"},
            {"id": 10, "op": "terminate", "bundle": "com.apple.Preferences"},
            {"id": 11, "op": "rotate"},
            {"id": 12, "op": "rotate"},
        ]
        replies, frames = asyncio.run(ws_session(cmds))
        check("video frames arrive", frames >= 5, f"{frames} frames")
        failed = [f"{c['op']}: {replies.get(c['id'], {}).get('err', 'no reply')}"
                  for c in cmds if not replies.get(c["id"], {}).get("ok")]
        check("all 10 kinds of command succeed", not failed, "; ".join(failed))
        calls = httpx.get(f"{FAKE}/calls").json()
        taps = [b for p, b in calls if p == "wda/tap"]
        check("tap lands on the exact center (188, 406)", taps and taps[0] == {"x": 188, "y": 406}, taps)
        keys = ["".join(b["value"]) for p, b in calls if p == "wda/keys"]
        check("text reaches the phone", "hi\n" in keys, keys)
        check("home button", any(p == "homescreen" for p, _ in calls))

        print("WDA settings")
        time.sleep(0.8)
        st = httpx.get(f"{FAKE}/settings").json()
        check("original settings restored after closing the panel",
              st.get("waitForIdleTimeout") == 10 and st.get("animationCoolOffTimeout") == 2, st)
        fast = [b for p, b in calls if p == "appium/settings" and b and b["settings"].get("waitForIdleTimeout") == 0]
        check("fast settings applied while watching", bool(fast))

        print("Security")
        try:
            asyncio.run(ws_session([{"id": 1, "op": "home"}], origin="http://evil.example"))
            check("other websites cannot connect", False, "connection was accepted")
        except Exception:
            check("other websites cannot connect", True)
        r = httpx.get(f"{WEB}/", headers={"Host": "evil.example"})
        check("unknown Host header rejected", r.status_code == 400, r.status_code)
        r = httpx.get(f"{WEB}/api/A/icon/..%2F..%2Fserver.py")
        check("icon path cannot escape its folder", r.status_code in (400, 404), r.status_code)

        print("Swipe shaping")
        from wda_client import _shape
        pts, hold = _shape([[0.5, 0.8, 0]] + [[0.5, 0.8 - 0.05 * i, i * 60] for i in range(1, 11)]
                           + [[0.5, 0.3, 700], [0.5, 0.3, 800]], 220)
        check("slow drag compressed to 220 ms and stops without inertia", pts[-1][2] <= 220 and hold == 100, (pts, hold))
        pts, hold = _shape([[0.5, 0.8, 0], [0.5, 0.3, 150]], 220)
        check("quick flick kept as is", pts[-1][2] == 150 and hold == 0, (pts, hold))
    finally:
        for p in (server, fake):
            if p:
                p.terminate()
                try:
                    p.wait(5)
                except Exception:
                    p.kill()
        try:
            cfg_file.unlink()
        except OSError:
            pass

    ok = sum(results)
    print(f"\n{ok}/{len(results)} checks passed" + ("  -> ALL GOOD" if ok == len(results) else "  -> SOMETHING FAILED"))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

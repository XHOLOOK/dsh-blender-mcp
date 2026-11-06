"""直接测试 bridge.py（绕过 MCP 层）。"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BLENDER = r"D:\Blender\blender.exe"
BRIDGE = os.path.join(ROOT, "bridge.py")

proc = subprocess.Popen([BLENDER, "--background", "--factory-startup", "--python", BRIDGE],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=ROOT)
pending = {}
lock = threading.Lock()


def reader():
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("@@MCP@@"):
            continue
        msg = json.loads(line[7:])
        rid = msg.get("id")
        if rid is None:
            print("[ready]", msg)
            continue
        ev = pending.pop(rid)
        ev.append(msg)


threading.Thread(target=reader, daemon=True).start()
threading.Thread(target=lambda: [sys.stderr.write(x.decode("utf-8", "replace")) for x in iter(proc.stderr.readline, b"")], daemon=True).start()


def call(method, params=None, timeout=300):
    import uuid
    rid = uuid.uuid4().hex
    ev = []
    pending[rid] = ev
    proc.stdin.write((json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n").encode())
    proc.stdin.flush()
    t0 = time.time()
    while not ev and time.time() - t0 < timeout:
        time.sleep(0.02)
    if not ev:
        raise TimeoutError(method)
    msg = ev[0]
    if not msg.get("ok"):
        print("FAIL", method, msg.get("error"))
        if msg.get("trace"):
            print(msg["trace"])
        return None
    return msg["result"]


def step(name, method, params=None):
    t0 = time.time()
    r = call(method, params)
    print("== %-22s %5.2fs %s" % (name, time.time() - t0, json.dumps(r, ensure_ascii=False)[:400]))
    return r


step("status", "status")
step("reset", "scene_reset", {})
step("gear", "preset", {"preset": "gear", "name": "Gear", "params": {"teeth": 20, "pitch_radius": 1.0, "thickness": 0.24, "bore_radius": 0.22}})
step("bolt", "preset", {"preset": "bolt", "name": "Bolt", "params": {"shaft_radius": 0.1, "length": 1.2}, "location": [2.4, 0, 0]})
step("vase", "preset", {"preset": "vase", "name": "Vase", "params": {"height": 2.0}, "location": [-2.6, 0, 0]})
step("knot", "preset", {"preset": "torus_knot", "name": "Knot", "params": {"p": 2, "q": 3, "minor_radius": 0.16}, "location": [0, 3, 1.2]})
step("material", "material", {"material": "Brass", "color": [0.85, 0.62, 0.24], "metallic": 0.95, "roughness": 0.28, "targets": ["Gear", "Bolt"]})
step("render", "render", {"resolution": 512, "samples": 16, "filepath": os.path.join(ROOT, "output", "bridge_test.png")})
step("export", "export", {"format": "glb", "filepath": os.path.join(ROOT, "output", "bridge_test.glb")})
step("objects", "list_objects", {})
print("DONE")
proc.stdin.close()
proc.wait(timeout=30)

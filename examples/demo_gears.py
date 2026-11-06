"""示例：通过 MCP 构建一组啮合传动的齿轮（模数相同，真实啮合几何）。

运行：
    python examples/demo_gears.py
产物：
    output/demo_gears.png / .glb / .blend
"""
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = r"D:\Blender\5.2\python\bin\python.exe"
SERVER = os.path.join(ROOT, "server.py")
OUT = os.path.join(ROOT, "output")


class MCPClient:
    def __init__(self):
        self.proc = subprocess.Popen([PY, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=ROOT)
        self._id = 0

    def _rpc(self, method, params=None, timeout=900):
        self._id += 1
        rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server 退出")
            obj = json.loads(line.decode("utf-8", "replace"))
            if obj.get("id") == rid:
                return obj
        raise TimeoutError(method)

    def init(self):
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "demo", "version": "1"}})
        notify = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self.proc.stdin.write((json.dumps(notify) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def call(self, name, args=None):
        r = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            raise RuntimeError(r["error"])
        res = r["result"]
        if res.get("isError"):
            raise RuntimeError(res["content"][0]["text"])
        for block in res["content"]:
            if block["type"] == "text":
                try:
                    return json.loads(block["text"])
                except Exception:
                    continue
        return res

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


def main():
    c = MCPClient()
    c.init()
    print("status:", c.call("blender_status"))
    c.call("scene_reset", {})

    M = 0.10  # 模数：pitch_radius = M * z / 2
    gears = [
        # name,   teeth, x,     y,     z_rot_deg, material
        ("Gear_Large", 24, 0.00, 0.00, 0.0),
        ("Gear_Mid", 16, 2.00, 0.00, 180.0 / 16),
        ("Gear_Small", 12, 2.70, math.sqrt(1.4 ** 2 - 0.7 ** 2), 180.0 / 16 + 180.0 / 12),
    ]
    for name, z, x, y, rot in gears:
        r = M * z / 2.0
        c.call("create_preset_model", {
            "preset": "gear", "name": name,
            "params": {"teeth": z, "pitch_radius": r, "thickness": 0.20,
                       "tooth_height": M * 1.8, "bore_radius": 0.16},
            "location": [x, y, 0.10], "rotation_deg": [0, 0, rot],
            "material": "Brass", "color": [0.82, 0.60, 0.22], "metallic": 0.95, "roughness": 0.30,
        })
        print("gear:", name, "r=%.3f" % r)

    # 轴 + 底板
    for name, z, x, y, rot in gears:
        c.call("add_primitive", {
            "type": "cylinder", "name": name + "_Shaft", "vertices": 32,
            "radius": 0.155, "depth": 0.78, "location": [x, y, -0.14],
            "material": "Steel", "color": [0.66, 0.68, 0.72], "metallic": 0.9, "roughness": 0.25,
        })
    c.call("add_primitive", {
        "type": "cube", "name": "Base", "size": 1.0, "scale": [4.3, 2.7, 0.05],
        "location": [1.35, 0.65, -0.55],
        "material": "BaseMetal", "color": [0.012, 0.014, 0.016], "metallic": 0.35, "roughness": 0.45,
    })
    print("parts:", len(c.call("list_objects")["objects"]))

    png = os.path.join(OUT, "demo_gears.png")
    print("render:", c.call("render_preview", {"resolution": 1280, "resolution_y": 960,
                                               "samples": 128, "engine": "EEVEE",
                                               "filepath": png}).get("file"))
    print("export:", c.call("export_model", {"format": "glb",
                                             "filepath": os.path.join(OUT, "demo_gears.glb")})["bytes"], "bytes")
    print("blend:", c.call("save_blend", {"filepath": os.path.join(OUT, "demo_gears.blend")})["file"])
    c.close()


if __name__ == "__main__":
    main()

"""示例：静物——陶瓷花瓶 + 黄铜环面纽结。

运行：
    python examples/demo_still.py
产物：
    output/demo_still.png / .glb / .blend
"""
import json
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
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
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

    # 台面
    c.call("add_primitive", {
        "type": "cube", "name": "Table", "size": 1.0, "scale": [3.2, 2.3, 0.05],
        "location": [0.0, 0.0, -1.03],
        "material": "TableTop", "color": [0.020, 0.019, 0.018], "metallic": 0.15, "roughness": 0.42,
    })

    # 陶瓷花瓶（旋纹）
    c.call("create_preset_model", {
        "preset": "vase", "name": "Vase",
        "params": {"height": 1.85, "segments": 96, "thickness": 0.035,
                   "profile": [[0.30, 0.00], [0.42, 0.03], [0.56, 0.22], [0.62, 0.62],
                               [0.58, 1.00], [0.40, 1.38], [0.30, 1.62], [0.33, 1.80],
                               [0.37, 1.85]]},
        "location": [-0.55, 0.05, -0.98],
        "material": "Porcelain", "color": [0.88, 0.86, 0.82], "metallic": 0.0, "roughness": 0.28,
    })
    print("vase ok")

    # 黄铜环面纽结
    c.call("create_preset_model", {
        "preset": "torus_knot", "name": "Knot",
        "params": {"p": 2, "q": 3, "major_radius": 0.30, "minor_radius": 0.072,
                   "segments": 360},
        "location": [1.30, 0.50, -0.53],
        "material": "Brass", "color": [0.80, 0.58, 0.24], "metallic": 1.0, "roughness": 0.26,
    })
    print("knot ok")

    # 底座
    c.call("add_primitive", {
        "type": "cylinder", "name": "Plinth", "vertices": 64, "radius": 0.34, "depth": 0.06,
        "location": [1.30, 0.50, -0.99],
        "material": "Stone", "color": [0.045, 0.045, 0.048], "metallic": 0.1, "roughness": 0.55,
    })

    print("parts:", len(c.call("list_objects")["objects"]))

    png = os.path.join(OUT, "demo_still.png")
    print("render:", c.call("render_preview", {"resolution": 1280, "resolution_y": 960,
                                               "samples": 128, "engine": "EEVEE",
                                               "filepath": png}).get("file"))
    print("export:", c.call("export_model", {"format": "glb",
                                             "filepath": os.path.join(OUT, "demo_still.glb")})["bytes"], "bytes")
    print("blend:", c.call("save_blend", {"filepath": os.path.join(OUT, "demo_still.blend")})["file"])
    c.close()


if __name__ == "__main__":
    main()

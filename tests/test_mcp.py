"""MCP 端到端测试：走完整 MCP 协议（initialize / tools/list / tools/call）。"""
import base64
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = r"D:\Blender\5.2\python\bin\python.exe"
SERVER = os.path.join(ROOT, "server.py")

proc = subprocess.Popen([PY, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, cwd=ROOT)
_id = [0]


def rpc(method, params=None, timeout=600):
    _id[0] += 1
    rid = _id[0]
    msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server 退出")
        obj = json.loads(line.decode("utf-8", "replace"))
        if obj.get("id") == rid:
            return obj
    raise TimeoutError(method)


def notify(method, params=None):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()


def call_tool(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    if "error" in r:
        print("  ERROR", r["error"])
        return None
    res = r["result"]
    if res.get("isError"):
        print("  TOOL_ERROR", res["content"][0]["text"][:400])
        return None
    return res


def show(label, res, save_image=None):
    if res is None:
        print("== %-24s FAILED" % label)
        return
    parts = []
    for block in res["content"]:
        if block["type"] == "text":
            parts.append(block["text"][:300])
        elif block["type"] == "image":
            raw = base64.b64decode(block["data"])
            print("  [image] %d bytes" % len(raw))
            if save_image:
                with open(save_image, "wb") as f:
                    f.write(raw)
                print("  [saved]", save_image)
    print("== %-24s %s" % (label, " | ".join(parts).replace("\n", " ")[:400]))


r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}})
print("initialize:", json.dumps(r["result"], ensure_ascii=False)[:300])
notify("notifications/initialized")

r = rpc("tools/list")
tools = r["result"]["tools"]
print("tools/list:", len(tools), [t["name"] for t in tools])
missing = [t["name"] for t in tools if "inputSchema" not in t or "description" not in t]
print("schema gaps:", missing)

show("status", call_tool("blender_status", {}))
show("reset", call_tool("scene_reset", {}))
show("gear", call_tool("create_preset_model", {
    "preset": "gear", "name": "Gear_A",
    "params": {"teeth": 24, "pitch_radius": 1.0, "thickness": 0.22, "bore_radius": 0.22},
    "material": "Brass", "color": [0.86, 0.64, 0.26], "metallic": 0.95, "roughness": 0.28}))
show("bolt", call_tool("create_preset_model", {
    "preset": "bolt", "name": "Bolt_A", "params": {"shaft_radius": 0.1, "length": 1.3, "head_radius": 0.3},
    "location": [2.6, 0.4, 0.1], "material": "Steel", "color": [0.62, 0.64, 0.68],
    "metallic": 0.9, "roughness": 0.32}))
show("render", call_tool("render_preview", {"resolution": 640, "samples": 24,
                                            "filepath": os.path.join(ROOT, "output", "mcp_test.png")}),
     save_image=os.path.join(ROOT, "output", "mcp_test_decoded.png"))
show("export", call_tool("export_model", {"format": "glb",
                                          "filepath": os.path.join(ROOT, "output", "mcp_test.glb")}))
show("python", call_tool("run_blender_python", {"code":
    "import bpy\nprint('objects', len(bpy.data.objects))\nRESULT = {'objs': [o.name for o in bpy.data.objects]}"}))

proc.stdin.close()
proc.wait(timeout=60)
print("DONE")

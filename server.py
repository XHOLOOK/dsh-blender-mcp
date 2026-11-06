#!/usr/bin/env python3
"""Blender MCP Server — 通过 MCP (Model Context Protocol) 控制本机 Blender 建模。

协议：MCP over stdio（换行分隔的 JSON-RPC 2.0）
后端：常驻 Blender 进程（--background），由 bridge.py 在 Blender 内执行 bpy 操作。
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "bridge.py")
LOG_DIR = os.path.join(HERE, "output")
os.makedirs(LOG_DIR, exist_ok=True)

SERVER_NAME = "blender-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_TIMEOUT = float(os.environ.get("BLENDER_MCP_TIMEOUT", "600"))

CANDIDATE_BLENDER = [
    os.environ.get("BLENDER_EXE"),
    r"D:\Blender\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender\blender.exe",
]


def find_blender():
    for p in CANDIDATE_BLENDER:
        if p and os.path.isfile(p):
            return p
    raise RuntimeError("未找到 blender.exe，请设置环境变量 BLENDER_EXE 指向 Blender 可执行文件")


# --------------------------------------------------------------------------- #
# Blender 后端进程
# --------------------------------------------------------------------------- #
class BlenderBackend:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.pending = {}
        self.ready = threading.Event()
        self.stderr_tail = []
        self.exe = None

    def _ensure(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        self.exe = find_blender()
        startupinfo = None
        flags = 0
        if sys.platform == "win32":
            flags = 0x08000000  # CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self.ready.clear()
        self.stderr_tail = []
        self.proc = subprocess.Popen(
            [self.exe, "--background", "--factory-startup", "--python", BRIDGE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=HERE, creationflags=flags, startupinfo=startupinfo,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err_reader, daemon=True).start()
        if not self.ready.wait(120):
            self.kill()
            raise RuntimeError("Blender 启动超时（120s）")
        self.alive_since = time.time()

    def _reader(self):
        proc = self.proc
        for raw in iter(proc.stdout.readline, b""):
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("@@MCP@@"):
                continue
            try:
                msg = json.loads(line[7:])
            except Exception:
                continue
            rid = msg.get("id")
            if rid is None:
                if msg.get("ok"):
                    self.ready.set()
                continue
            item = self.pending.pop(rid, None)
            if item:
                item["msg"] = msg
                item["event"].set()

    def _err_reader(self):
        proc = self.proc
        path = os.path.join(LOG_DIR, "blender_stderr.log")
        try:
            with open(path, "ab") as log:
                for raw in iter(proc.stderr.readline, b""):
                    if not raw:
                        break
                    try:
                        log.write(raw)
                        log.flush()
                    except Exception:
                        pass
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    self.stderr_tail.append(text)
                    if len(self.stderr_tail) > 200:
                        del self.stderr_tail[:50]
        except Exception:
            pass

    def call(self, method, params=None, timeout=None):
        timeout = timeout or DEFAULT_TIMEOUT
        with self.lock:
            self._ensure()
            rid = uuid.uuid4().hex
            item = {"event": threading.Event(), "msg": None}
            self.pending[rid] = item
            payload = json.dumps({"id": rid, "method": method, "params": params or {}},
                                 ensure_ascii=False) + "\n"
            try:
                self.proc.stdin.write(payload.encode("utf-8"))
                self.proc.stdin.flush()
            except Exception:
                self.kill()
                raise RuntimeError("无法写入 Blender 进程，已重启")

            if not item["event"].wait(timeout):
                self.pending.pop(rid, None)
                self.kill()
                raise TimeoutError("Blender 调用 %s 超时（%ss）" % (method, timeout))
            msg = item["msg"] or {}
            if not msg.get("ok"):
                err = msg.get("error") or "未知错误"
                trace = msg.get("trace")
                if trace:
                    raise RuntimeError(err + "\n" + trace)
                raise RuntimeError(err)
            return msg.get("result")

    def kill(self):
        proc, self.proc = self.proc, None
        self.pending.clear()
        self.ready.clear()
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def shutdown(self):
        try:
            self.kill()
        except Exception:
            pass


BACKEND = BlenderBackend()


# --------------------------------------------------------------------------- #
# 工具定义
# --------------------------------------------------------------------------- #
def vec3(desc, default=None):
    schema = {"type": "array", "description": desc, "minItems": 3, "maxItems": 3,
              "items": {"type": "number"}}
    if default is not None:
        schema["default"] = default
    return schema


TOOLS = [
    {
        "name": "blender_status",
        "description": "查看 Blender 版本、场景对象/三角面/材质数量、渲染引擎与当前文件等状态。用于确认后端可用。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "scene_reset",
        "description": "清空当前场景的所有对象（可选保留相机/灯光），开始一次全新的建模。",
        "inputSchema": {
            "type": "object",
            "properties": {"keep_camera": {"type": "boolean", "default": False,
                                           "description": "保留现有相机与灯光"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_objects",
        "description": "列出场景中所有对象及其变换、尺寸、顶点数、材质。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "add_primitive",
        "description": "添加基础几何体到场景：cube/sphere/ico_sphere/cylinder/cone/torus/plane/monkey/circle。可同时指定位置、旋转、缩放和材质。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["cube", "sphere", "ico_sphere", "cylinder",
                                                    "cone", "torus", "plane", "monkey", "circle"],
                         "default": "cube"},
                "name": {"type": "string", "description": "对象名称"},
                "location": vec3("世界坐标位置 [x,y,z]", [0, 0, 0]),
                "rotation_deg": vec3("欧拉旋转（度）", [0, 0, 0]),
                "scale": vec3("缩放", [1, 1, 1]),
                "size": {"type": "number", "description": "cube/plane/monkey 的尺寸"},
                "radius": {"type": "number", "description": "球/圆柱/圆的半径"},
                "depth": {"type": "number", "description": "圆柱/圆锥的高度"},
                "vertices": {"type": "integer", "description": "圆柱/圆锥/圆的边数"},
                "segments": {"type": "integer", "description": "UV 球的经线段数"},
                "ring_count": {"type": "integer", "description": "UV 球的纬线段数"},
                "subdivisions": {"type": "integer", "description": "ico 球细分级别"},
                "radius1": {"type": "number", "description": "圆锥底部半径"},
                "radius2": {"type": "number", "description": "圆锥顶部半径（0 为尖顶）"},
                "major_radius": {"type": "number", "description": "圆环主半径"},
                "minor_radius": {"type": "number", "description": "圆环管半径"},
                "material": {"type": "string", "description": "材质名称"},
                "color": {"type": "array", "items": {"type": "number"}, "description": "基础色 [r,g,b] 或 [r,g,b,a]"},
                "metallic": {"type": "number", "description": "金属度 0-1"},
                "roughness": {"type": "number", "description": "粗糙度 0-1"},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "transform_object",
        "description": "设置对象的位移/旋转/缩放，或相对位移，可应用缩放。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "对象名称"},
                "location": vec3("绝对位置"),
                "rotation_deg": vec3("欧拉旋转（度）"),
                "scale": vec3("缩放"),
                "delta_location": vec3("相对位移"),
                "apply_scale": {"type": "boolean", "description": "应用缩放"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_objects",
        "description": "按名称批量删除对象。",
        "inputSchema": {
            "type": "object",
            "properties": {"names": {"type": "array", "items": {"type": "string"}}},
            "required": ["names"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_material",
        "description": "创建/更新 PBR 材质并赋予目标对象（基础色、金属度、粗糙度、自发光、透射）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "material": {"type": "string", "description": "材质名称（不存在则新建）"},
                "targets": {"type": "array", "items": {"type": "string"},
                            "description": "目标对象名列表；省略则作用于所有网格对象"},
                "target": {"type": "string", "description": "单个目标对象（targets 的简写）"},
                "color": {"type": "array", "items": {"type": "number"}},
                "metallic": {"type": "number"},
                "roughness": {"type": "number"},
                "emission": {"type": "array", "items": {"type": "number"}},
                "emission_strength": {"type": "number"},
                "transmission": {"type": "number"},
            },
            "required": ["material"],
            "additionalProperties": False,
        },
    },
    {
        "name": "boolean_operation",
        "description": "对两个对象执行布尔运算（difference/union/intersect）并应用结果，可删除切割体。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "被操作对象"},
                "cutter": {"type": "string", "description": "切割体对象"},
                "operation": {"type": "string", "enum": ["difference", "union", "intersect"],
                              "default": "difference"},
                "delete_cutter": {"type": "boolean", "default": True},
            },
            "required": ["target", "cutter"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_modifier",
        "description": "添加修改器：bevel/subsurf/array/mirror/solidify/wireframe/displace/triangulate，可应用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "对象名称"},
                "type": {"type": "string", "enum": ["bevel", "subsurf", "array", "mirror",
                                                    "solidify", "wireframe", "displace", "triangulate"]},
                "params": {"type": "object", "description": "修改器属性，例如 {width:0.02, segments:3}"},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["name", "type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_preset_model",
        "description": "用内置参数化生成器创建程序化模型：gear（直齿轮）/bolt（六角螺栓）/vase（旋转体花瓶）/torus_knot（环面纽结）/tree（低模树）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "enum": ["gear", "bolt", "vase", "torus_knot", "tree"]},
                "name": {"type": "string", "description": "对象名称"},
                "params": {"type": "object", "description": "生成参数"},
                "location": vec3("位置"),
                "rotation_deg": vec3("旋转（度）"),
                "material": {"type": "string"},
                "color": {"type": "array", "items": {"type": "number"}},
                "metallic": {"type": "number"},
                "roughness": {"type": "number"},
            },
            "required": ["preset"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_preview",
        "description": "自动布光并取景，渲染场景预览图（EEVEE/Cycles），返回图片与文件路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resolution": {"type": "integer", "default": 1024},
                "resolution_y": {"type": "integer"},
                "samples": {"type": "integer", "default": 32},
                "engine": {"type": "string", "enum": ["EEVEE", "CYCLES", "WORKBENCH"], "default": "EEVEE"},
                "transparent": {"type": "boolean", "default": False},
                "camera": {"type": "string", "description": "指定已存在的相机对象名"},
                "filepath": {"type": "string", "description": "输出 PNG 路径"},
                "frame": {"type": "integer"},
                "return_image": {"type": "boolean", "default": True,
                                 "description": "是否把图片内容内联返回（false 只返回路径）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "export_model",
        "description": "导出模型文件：glb/gltf/obj/fbx/stl，可仅导出指定对象。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "输出路径"},
                "format": {"type": "string", "enum": ["glb", "gltf", "obj", "fbx", "stl"], "default": "glb"},
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "只导出这些对象；省略则导出整个场景"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "save_blend",
        "description": "保存当前场景为 .blend 工程文件。",
        "inputSchema": {
            "type": "object",
            "properties": {"filepath": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "run_blender_python",
        "description": "在 Blender 内执行任意 Python/bpy 代码（持久场景），返回 stdout 与 RESULT 变量。用于复杂建模、动画、节点等高级操作。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "要执行的 Python 代码"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
]

TOOL_METHOD = {
    "blender_status": "status",
    "scene_reset": "scene_reset",
    "list_objects": "list_objects",
    "add_primitive": "add_primitive",
    "transform_object": "transform",
    "delete_objects": "delete",
    "set_material": "material",
    "boolean_operation": "boolean",
    "add_modifier": "modifier",
    "create_preset_model": "preset",
    "render_preview": "render",
    "export_model": "export",
    "save_blend": "save_blend",
    "run_blender_python": "run_python",
}


# --------------------------------------------------------------------------- #
# MCP 处理
# --------------------------------------------------------------------------- #
def jsonrpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def jsonrpc_error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def text_content(text):
    return {"type": "text", "text": text}


def tool_error(message):
    return {"content": [text_content(message)], "isError": True}


def handle_tools_call(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    method = TOOL_METHOD.get(name)
    if method is None:
        return tool_error("未知工具: %s" % name)

    return_image = bool(args.get("return_image", True))
    call_args = dict(args)
    call_args.pop("return_image", None)

    try:
        result = BACKEND.call(method, call_args)
    except Exception as exc:
        return tool_error("Blender 执行失败: %s" % exc)

    blocks = []
    if method == "render" and isinstance(result, dict) and result.get("file"):
        path = result["file"]
        summary = ("渲染完成：%s\n分辨率 %sx%s，引擎 %s，耗时 %ss"
                   % (path, result.get("width"), result.get("height"),
                      result.get("engine"), result.get("seconds")))
        blocks.append(text_content(json.dumps(result, ensure_ascii=False)))
        blocks.append(text_content(summary))
        if return_image and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
                if size <= 8 * 1024 * 1024:
                    with open(path, "rb") as fh:
                        b64 = base64.b64encode(fh.read()).decode("ascii")
                    blocks.append({"type": "image", "data": b64, "mimeType": "image/png"})
                else:
                    blocks.append(text_content("图片过大（%.1f MB），未内联，请直接打开文件。"
                                               % (size / 1048576.0)))
            except Exception as exc:
                blocks.append(text_content("读取图片失败: %s" % exc))
        return {"content": blocks, "isError": False}

    blocks.append(text_content(json.dumps(result, ensure_ascii=False, indent=2)))
    return {"content": blocks, "isError": False}


def handle_message(msg):
    if not isinstance(msg, dict):
        return jsonrpc_error(None, -32600, "Invalid Request")
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method is None:
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
        return jsonrpc_result(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "通过本服务在 Blender 中建模：基础图元、布尔、修改器、参数化预设、渲染预览与模型导出。",
        })

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return jsonrpc_result(rid, {})

    if method == "tools/list":
        return jsonrpc_result(rid, {"tools": TOOLS})

    if method == "tools/call":
        try:
            return jsonrpc_result(rid, handle_tools_call(params))
        except Exception as exc:
            return jsonrpc_error(rid, -32603, "内部错误: %s" % exc)

    if method == "resources/list":
        return jsonrpc_result(rid, {"resources": []})
    if method == "prompts/list":
        return jsonrpc_result(rid, {"prompts": []})

    return jsonrpc_error(rid, -32601, "Method not found: %s" % method)


def write_message(obj):
    data = json.dumps(obj, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.buffer.flush()


def serve_stdio():
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            write_message(jsonrpc_error(None, -32700, "Parse error"))
            continue
        if isinstance(msg, list):
            responses = [r for r in (handle_message(m) for m in msg) if r]
            if responses:
                write_message(responses)
        else:
            resp = handle_message(msg)
            if resp is not None:
                write_message(resp)


def selftest():
    print("[selftest] blender:", find_blender())
    print("[selftest] status:", json.dumps(BACKEND.call("status"), ensure_ascii=False))
    print("[selftest] primitive:", json.dumps(
        BACKEND.call("add_primitive", {"type": "torus", "name": "SelfTest_Torus",
                                       "major_radius": 1.2, "minor_radius": 0.35,
                                       "location": [0, 0, 1.5]}), ensure_ascii=False))
    out = BACKEND.call("render", {"resolution": 320, "samples": 8,
                                  "filepath": os.path.join(LOG_DIR, "selftest.png")})
    print("[selftest] render:", json.dumps(out, ensure_ascii=False))
    BACKEND.shutdown()
    print("[selftest] OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    try:
        serve_stdio()
    finally:
        BACKEND.shutdown()


if __name__ == "__main__":
    main()

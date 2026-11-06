"""Blender 端运行时桥 (bridge)

由 MCP server 以如下方式启动：
    blender.exe --background --factory-startup --python bridge.py

从 stdin 读取一行一个 JSON 请求 {id, method, params}，
在 Blender 主线程内执行 bpy 操作，并通过 stdout 写回一行：
    @@MCP@@{"id":..., "ok":true, "result":...}
其他 Blender 日志不会带 @@MCP@@ 前缀，被 server 端过滤。
"""

import sys
import os
import io
import json
import math
import time
import traceback
import contextlib

import bpy
from mathutils import Vector, Euler

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import genlib
except Exception:  # genlib 可选
    genlib = None

OUT_DIR = os.path.join(HERE, "output")
os.makedirs(OUT_DIR, exist_ok=True)

VERSION = bpy.app.version_string


# --------------------------------------------------------------------------- #
# 通信
# --------------------------------------------------------------------------- #
def _emit(payload):
    data = "@@MCP@@" + json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.buffer.flush()


def _readline():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return line.decode("utf-8", errors="replace").strip()


# --------------------------------------------------------------------------- #
# 场景工具
# --------------------------------------------------------------------------- #
def _scene():
    return bpy.context.scene


def _objects():
    return list(_scene().objects)


def _mesh_objects():
    return [o for o in _scene().objects if o.type == "MESH"]


def _get_obj(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError("对象不存在: %s" % name)
    return obj


def _select_only(obj):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _vec(p, default=(0.0, 0.0, 0.0)):
    if p is None:
        return Vector(default)
    return Vector([float(x) for x in p])


def _deg(p):
    return Euler([math.radians(float(x)) for x in p], "XYZ")


def _obj_info(o):
    return {
        "name": o.name,
        "type": o.type,
        "location": [round(v, 5) for v in o.location],
        "rotation_deg": [round(math.degrees(v), 3) for v in o.rotation_euler],
        "scale": [round(v, 5) for v in o.scale],
        "dimensions": [round(v, 5) for v in o.dimensions],
        "vertices": len(o.data.vertices) if o.type == "MESH" else 0,
        "materials": [m.name for m in o.data.materials if m] if o.type == "MESH" else [],
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def h_ping(params):
    return {"pong": True, "time": time.time()}


def h_status(params):
    sc = _scene()
    meshes = _mesh_objects()
    tris = 0
    for o in meshes:
        try:
            tris += len(o.data.loop_triangles)
        except Exception:
            pass
    return {
        "blender": VERSION,
        "python": sys.version.split()[0],
        "engine": sc.render.engine,
        "objects": len(sc.objects),
        "meshes": len(meshes),
        "triangles": tris,
        "materials": len(bpy.data.materials),
        "camera": sc.camera.name if sc.camera else None,
        "frames": [sc.frame_start, sc.frame_end],
        "output_dir": OUT_DIR,
        "file": bpy.data.filepath or None,
    }


def h_scene_reset(params):
    keep = bool(params.get("keep_camera", False))
    removed = 0
    for o in _objects():
        if keep and o.type in {"CAMERA", "LIGHT"}:
            continue
        bpy.data.objects.remove(o, do_unlink=True)
        removed += 1
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.curves):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)
    return {"removed": removed}


def h_list_objects(params):
    return {"objects": [_obj_info(o) for o in _objects()]}


PRIMITIVES = {
    "cube": {"op": "primitive_cube_add", "args": {"size": "size"}},
    "sphere": {"op": "primitive_uv_sphere_add", "args": {"radius": "radius", "segments": "segments", "ring_count": "ring_count"}},
    "ico_sphere": {"op": "primitive_ico_sphere_add", "args": {"radius": "radius", "subdivisions": "subdivisions"}},
    "cylinder": {"op": "primitive_cylinder_add", "args": {"radius": "radius", "depth": "depth", "vertices": "vertices"}},
    "cone": {"op": "primitive_cone_add", "args": {"radius1": "radius1", "radius2": "radius2", "depth": "depth", "vertices": "vertices"}},
    "torus": {"op": "primitive_torus_add", "args": {"major_radius": "major_radius", "minor_radius": "minor_radius", "major_segments": "major_segments", "minor_segments": "minor_segments"}},
    "plane": {"op": "primitive_plane_add", "args": {"size": "size"}},
    "monkey": {"op": "primitive_monkey_add", "args": {"size": "size"}},
    "circle": {"op": "primitive_circle_add", "args": {"radius": "radius", "vertices": "vertices"}},
}


def h_add_primitive(params):
    ptype = (params.get("type") or "cube").lower()
    if ptype not in PRIMITIVES:
        raise ValueError("不支持的图元: %s (可选: %s)" % (ptype, ", ".join(PRIMITIVES)))
    spec = PRIMITIVES[ptype]
    kwargs = {}
    for bpy_arg, pkey in spec["args"].items():
        if pkey in params and params[pkey] is not None:
            kwargs[bpy_arg] = params[pkey]
    kwargs["location"] = tuple(_vec(params.get("location")))
    if params.get("rotation_deg"):
        kwargs["rotation"] = tuple(_deg(params["rotation_deg"]))
    if params.get("scale"):
        kwargs["scale"] = tuple(_vec(params["scale"], (1, 1, 1)))
    getattr(bpy.ops.mesh, spec["op"])(**kwargs)
    obj = bpy.context.object
    if params.get("name"):
        obj.name = params["name"]
    if params.get("material"):
        _apply_material(obj, params["material"], params.get("color"), params.get("metallic"), params.get("roughness"))
    bpy.context.view_layer.update()
    return {"object": _obj_info(obj)}


def h_transform(params):
    obj = _get_obj(params["name"])
    if params.get("location") is not None:
        obj.location = _vec(params["location"])
    if params.get("rotation_deg") is not None:
        obj.rotation_euler = _deg(params["rotation_deg"])
    if params.get("scale") is not None:
        obj.scale = _vec(params["scale"], (1, 1, 1))
    if params.get("delta_location") is not None:
        obj.location = obj.location + _vec(params["delta_location"])
    if params.get("apply_scale"):
        _select_only(obj)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.context.view_layer.update()
    return {"object": _obj_info(obj)}


def h_delete(params):
    names = params.get("names") or []
    removed = []
    for n in names:
        obj = bpy.data.objects.get(n)
        if obj:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed.append(n)
    return {"removed": removed, "missing": [n for n in names if n not in removed]}


def _apply_material(obj, name=None, color=None, metallic=None, roughness=None, emission=None):
    mat = bpy.data.materials.get(name) if name else None
    if mat is None:
        mat = bpy.data.materials.new(name or (obj.name + "_Mat"))
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            if color:
                bsdf.inputs["Base Color"].default_value = tuple(
                    (list(color) + [1.0])[:4] if len(color) < 4 else color[:4])
            if metallic is not None:
                bsdf.inputs["Metallic"].default_value = float(metallic)
            if roughness is not None:
                bsdf.inputs["Roughness"].default_value = float(roughness)
            if emission:
                try:
                    bsdf.inputs["Emission Color"].default_value = tuple(
                        (list(emission) + [1.0])[:4] if len(emission) < 4 else emission[:4])
                    bsdf.inputs["Emission Strength"].default_value = float(params_strength(emission))
                except Exception:
                    pass
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat


def params_strength(_):
    return 1.5


def h_material(params):
    name = params.get("material") or params.get("name")
    if not name:
        raise ValueError("需要 material 名称")
    targets = params.get("targets") or ([params["target"]] if params.get("target") else [])
    if not targets:
        targets = [o.name for o in _mesh_objects()]
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        raise ValueError("材质缺少 Principled BSDF 节点")
    if params.get("color") is not None:
        c = list(params["color"])
        bsdf.inputs["Base Color"].default_value = (c + [1.0])[:4] if len(c) < 4 else c[:4]
    if params.get("metallic") is not None:
        bsdf.inputs["Metallic"].default_value = float(params["metallic"])
    if params.get("roughness") is not None:
        bsdf.inputs["Roughness"].default_value = float(params["roughness"])
    if params.get("emission") is not None:
        try:
            e = list(params["emission"])
            bsdf.inputs["Emission Color"].default_value = (e + [1.0])[:4] if len(e) < 4 else e[:4]
            bsdf.inputs["Emission Strength"].default_value = float(params.get("emission_strength", 2.0))
        except Exception:
            pass
    if params.get("transmission") is not None:
        try:
            bsdf.inputs["Transmission Weight"].default_value = float(params["transmission"])
        except Exception:
            pass
    applied = []
    for tname in targets:
        o = bpy.data.objects.get(tname)
        if o and o.type == "MESH":
            if o.data.materials:
                o.data.materials[0] = mat
            else:
                o.data.materials.append(mat)
            applied.append(tname)
    return {"material": mat.name, "applied_to": applied}


def h_boolean(params):
    target = _get_obj(params["target"])
    cutter = _get_obj(params["cutter"])
    op = params.get("operation", "difference").upper()
    if op not in {"DIFFERENCE", "UNION", "INTERSECT"}:
        raise ValueError("operation 必须是 difference/union/intersect")
    _select_only(target)
    mod = target.modifiers.new(name="MCPBoolean", type="BOOLEAN")
    mod.operation = op
    mod.object = cutter
    try:
        mod.solver = "EXACT"
    except Exception:
        pass
    bpy.ops.object.modifier_apply(modifier=mod.name)
    if params.get("delete_cutter", True):
        bpy.data.objects.remove(cutter, do_unlink=True)
    bpy.context.view_layer.update()
    return {"object": _obj_info(target)}


MODIFIER_TYPES = {"bevel", "subsurf", "array", "mirror", "solidify", "wireframe", "displace", "triangulate"}


def h_modifier(params):
    obj = _get_obj(params["name"])
    mtype = params.get("type", "").lower()
    if mtype == "subsurf":
        bpy_type = "SUBSURF"
    elif mtype in MODIFIER_TYPES:
        bpy_type = mtype.upper()
    else:
        raise ValueError("不支持的修改器: %s" % mtype)
    mod = obj.modifiers.new(name=params.get("modifier_name", "MCP_" + bpy_type), type=bpy_type)
    for key, value in (params.get("params") or {}).items():
        try:
            prop = mod.bl_rna.properties[key]
            if prop.type == "ENUM":
                setattr(mod, key, str(value))
            else:
                setattr(mod, key, value)
        except Exception:
            pass
    if params.get("apply"):
        _select_only(obj)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.context.view_layer.update()
    return {"object": _obj_info(obj), "modifier": mod.name}


PRESETS = {
    "gear": genlib.gear if genlib else None,
    "bolt": genlib.hex_bolt if genlib else None,
    "vase": genlib.vase if genlib else None,
    "torus_knot": genlib.torus_knot if genlib else None,
    "tree": genlib.lowpoly_tree if genlib else None,
}


def h_preset(params):
    preset = (params.get("preset") or "").lower()
    fn = PRESETS.get(preset)
    if fn is None:
        raise ValueError("未知预设: %s (可选: %s)" % (preset, ", ".join(PRESETS)))
    p = dict(params.get("params") or {})
    if params.get("name"):
        p["name"] = params["name"]
    obj = fn(**p)
    if params.get("location"):
        obj.location = _vec(params["location"])
    if params.get("rotation_deg"):
        obj.rotation_euler = _deg(params["rotation_deg"])
    if params.get("material"):
        mat_params = {"material": params["material"], "target": obj.name,
                      "color": params.get("color"), "metallic": params.get("metallic", 0.8),
                      "roughness": params.get("roughness", 0.35)}
        h_material(mat_params)
    bpy.context.view_layer.update()
    return {"object": _obj_info(obj)}


# ------------------------------- 渲染 -------------------------------------- #
def _scene_bounds():
    pts = []
    for o in _mesh_objects():
        for corner in o.bound_box:
            pts.append(o.matrix_world @ Vector(corner))
    if not pts:
        return Vector((0, 0, 0)), 1.0
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    center = Vector(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2))
    radius = max(0.5, 0.5 * math.sqrt((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2))
    return center, radius


def _ensure_camera(center, radius, direction=(1.00, -1.15, 0.52), name="MCP_Camera"):
    cam = bpy.data.objects.get(name)
    if cam is None:
        data = bpy.data.cameras.new(name)
        cam = bpy.data.objects.new(name, data)
        _scene().collection.objects.link(cam)
    data = cam.data
    data.lens = 50.0
    data.sensor_width = 36.0
    fov = 2.0 * math.atan(data.sensor_width / (2.0 * data.lens))
    dist = radius / math.tan(fov / 2.0) * 1.12
    d = Vector(direction).normalized()
    cam.location = center + d * dist
    quat = (center - cam.location).to_track_quat("-Z", "Y")
    cam.rotation_euler = quat.to_euler()
    _scene().camera = cam
    return cam


def _ensure_lights(center, radius):
    sc = _scene()
    existing = [o for o in sc.objects if o.type == "LIGHT"]
    if existing:
        return
    def add_area(name, offset, energy, size, color=(1, 1, 1)):
        data = bpy.data.lights.new(name, type="AREA")
        data.energy = energy * max(1.0, radius * radius)
        data.size = size * max(0.5, radius)
        data.color = color
        obj = bpy.data.objects.new(name, data)
        sc.collection.objects.link(obj)
        obj.location = center + Vector(offset) * max(0.8, radius)
        obj.rotation_euler = (center - obj.location).to_track_quat("-Z", "Y").to_euler()
        return obj
    add_area("Key", (2.4, -2.8, 2.8), 700, 3.0, (1.0, 0.96, 0.9))
    add_area("Fill", (-3.2, -1.8, 1.4), 220, 4.0, (0.82, 0.88, 1.0))
    add_area("Rim", (-0.8, 3.2, 2.6), 380, 2.5, (1.0, 1.0, 1.0))
    world = sc.world or bpy.data.worlds.new("World")
    sc.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.055, 0.060, 0.070, 1.0)
        bg.inputs[1].default_value = 1.0


def _set_engine(sc, engine, samples):
    engine = (engine or "EEVEE").upper()
    if engine in {"CYCLES"}:
        sc.render.engine = "CYCLES"
        try:
            sc.cycles.samples = samples
            sc.cycles.use_denoising = True
        except Exception:
            pass
    elif engine in {"WORKBENCH"}:
        sc.render.engine = "BLENDER_WORKBENCH"
    else:
        sc.render.engine = "BLENDER_EEVEE"
        for attr in ("taa_render_samples", "taa_samples", "samples"):
            if hasattr(sc.eevee, attr):
                try:
                    setattr(sc.eevee, attr, int(samples))
                    break
                except Exception:
                    pass
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Medium High Contrast"
    except Exception:
        try:
            sc.view_settings.view_transform = "Filmic"
        except Exception:
            pass


def h_render(params):
    sc = _scene()
    center, radius = _scene_bounds()
    if params.get("camera"):
        cam = _get_obj(params["camera"])
        sc.camera = cam
    elif sc.camera is None or params.get("auto_camera", True):
        _ensure_camera(center, radius)
    _ensure_lights(center, radius)

    _set_engine(sc, params.get("engine", "EEVEE"), int(params.get("samples", 32)))

    res = int(params.get("resolution", 1024))
    sc.render.resolution_x = res
    sc.render.resolution_y = int(params.get("resolution_y", res))
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = bool(params.get("transparent", False))
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA" if sc.render.film_transparent else "RGB"

    if params.get("frame") is not None:
        sc.frame_set(int(params["frame"]))

    out = params.get("filepath")
    if out:
        out = os.path.abspath(out)
    else:
        out = os.path.join(OUT_DIR, "render_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
        if os.path.exists(out):
            out = os.path.join(OUT_DIR, "render_%s_%d.png" % (time.strftime("%Y%m%d_%H%M%S"), int(time.time() * 1000) % 1000))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sc.render.filepath = out

    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    elapsed = round(time.time() - t0, 2)
    return {"file": out, "width": sc.render.resolution_x, "height": sc.render.resolution_y,
            "engine": sc.render.engine, "seconds": elapsed}


# ------------------------------- 导出 -------------------------------------- #
def h_export(params):
    fmt = (params.get("format") or "glb").lower()
    filepath = params.get("filepath")
    if not filepath:
        filepath = os.path.join(OUT_DIR, "model_" + time.strftime("%Y%m%d_%H%M%S") + "." + fmt)
    filepath = os.path.abspath(filepath)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    selection = params.get("names") or []
    if selection:
        bpy.ops.object.select_all(action="DESELECT")
        for n in selection:
            o = bpy.data.objects.get(n)
            if o:
                o.select_set(True)

    if fmt in {"glb", "gltf"}:
        kwargs = dict(filepath=filepath, export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE")
        if selection:
            try:
                bpy.ops.export_scene.gltf(use_selection=True, **kwargs)
            except TypeError:
                bpy.ops.export_scene.gltf(export_selected=True, **kwargs)
        else:
            bpy.ops.export_scene.gltf(**kwargs)
    elif fmt == "obj":
        try:
            bpy.ops.wm.obj_export(filepath=filepath, export_selected_objects=bool(selection))
        except Exception:
            bpy.ops.export_scene.obj(filepath=filepath, use_selection=bool(selection))
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=filepath, use_selection=bool(selection), apply_scale_options="FBX_SCALE_ALL")
    elif fmt == "stl":
        try:
            bpy.ops.wm.stl_export(filepath=filepath, export_selected_objects=bool(selection))
        except Exception:
            bpy.ops.export_mesh.stl(filepath=filepath, use_selection=bool(selection))
    else:
        raise ValueError("不支持的格式: %s (glb/gltf/obj/fbx/stl)" % fmt)

    size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    return {"file": filepath, "format": fmt, "bytes": size, "selected_only": bool(selection)}


def h_save_blend(params):
    filepath = params.get("filepath") or os.path.join(OUT_DIR, "scene_" + time.strftime("%Y%m%d_%H%M%S") + ".blend")
    filepath = os.path.abspath(filepath)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=filepath)
    return {"file": filepath, "bytes": os.path.getsize(filepath)}


def h_open_blend(params):
    filepath = os.path.abspath(params["filepath"])
    bpy.ops.wm.open_mainfile(filepath=filepath)
    return {"file": filepath, "objects": len(_objects())}


def h_run_python(params):
    code = params.get("code") or ""
    if not code.strip():
        raise ValueError("code 为空")
    buf = io.StringIO()
    g = {
        "__name__": "__mcp__",
        "bpy": bpy,
        "genlib": genlib,
        "math": math,
        "scene": _scene(),
        "MCP_OUT": OUT_DIR,
        "RESULT": None,
    }
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<mcp>", "exec"), g)
    except Exception as exc:
        out = buf.getvalue()
        raise RuntimeError("%s: %s" % (type(exc).__name__, exc) + ("\n--- stdout ---\n" + out if out else ""))
    out = buf.getvalue()
    result = g.get("RESULT")
    try:
        json.dumps(result)
    except Exception:
        result = repr(result)
    return {"stdout": out, "result": result}


HANDLERS = {
    "ping": h_ping,
    "status": h_status,
    "scene_reset": h_scene_reset,
    "list_objects": h_list_objects,
    "add_primitive": h_add_primitive,
    "transform": h_transform,
    "delete": h_delete,
    "material": h_material,
    "boolean": h_boolean,
    "modifier": h_modifier,
    "preset": h_preset,
    "render": h_render,
    "export": h_export,
    "save_blend": h_save_blend,
    "open_blend": h_open_blend,
    "run_python": h_run_python,
}


def handle(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    fn = HANDLERS.get(method)
    if fn is None:
        return {"id": rid, "ok": False, "error": "未知方法: %s" % method, "code": "unknown_method"}
    try:
        return {"id": rid, "ok": True, "result": fn(params)}
    except Exception as exc:
        return {"id": rid, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "trace": traceback.format_exc()}


def main():
    _emit({"id": None, "ok": True, "result": {"ready": True, "blender": VERSION}})
    while True:
        line = _readline()
        if line is None:
            break
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            _emit({"id": None, "ok": False, "error": "JSON 解析失败: %s" % exc})
            continue
        _emit(handle(req))


if __name__ == "__main__":
    main()

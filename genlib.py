"""参数化几何生成库 — Blender MCP 运行时 (genlib)

每个函数都在当前 Blender 场景中创建对象并返回 bpy.types.Object。
所有尺寸单位为 Blender 单位（1 = 1 m），Z 轴向上。
"""

import math
import bpy
import bmesh
from mathutils import Vector


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _link(obj, collection=None):
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def _mesh_from_pydata(name, verts, faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.validate(verbose=False)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    _link(obj)
    return obj


def _select_only(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _auto_smoothfill(obj, angle_deg=35.0):
    """Blender 5.x: shade auto smooth via 平滑组/法线修改器。"""
    try:
        _select_only(obj)
        bpy.ops.object.shade_auto_smooth(angle=math.radians(angle_deg))
    except Exception:
        try:
            _select_only(obj)
            bpy.ops.object.shade_smooth()
        except Exception:
            pass


def _boolean_cut(target, cutter):
    _select_only(target)
    mod = target.modifiers.new(name="BoolCut", type="BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.object = cutter
    try:
        mod.solver = "EXACT"
    except Exception:
        pass
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def _add_cutter_cylinder(radius, depth, location=(0, 0, 0), verts=64):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=depth, location=location)
    return bpy.context.object


# --------------------------------------------------------------------------- #
# 直齿圆柱齿轮
# --------------------------------------------------------------------------- #
def gear(teeth=18, pitch_radius=1.0, thickness=0.24, tooth_height=0.18,
         bore_radius=0.20, tip_ratio=0.30, root_ratio=0.58, name="Gear"):
    """直齿圆柱齿轮：梯形齿廓 + 直接构造的同轴孔环拓扑（不用布尔，网格干净）。"""
    teeth = max(6, int(teeth))
    r_tip = pitch_radius + tooth_height * 0.5
    r_root = pitch_radius - tooth_height * 0.5
    ang = 2.0 * math.pi / teeth
    tip_half = ang * 0.5 * tip_ratio
    root_half = ang * 0.5 * root_ratio

    pts = []
    for i in range(teeth):
        a = i * ang
        pts.append((r_root, a - root_half))
        pts.append((r_tip, a - tip_half))
        pts.append((r_tip, a + tip_half))
        pts.append((r_root, a + root_half))
        pts.append((r_root, a + root_half + (ang - 2 * root_half) * 0.5))
        pts.append((r_root, a + ang - root_half))
    clean = []
    for p in pts:
        if not clean or abs(p[1] - clean[-1][1]) > 1e-6:
            clean.append(p)
    pts = clean
    n = len(pts)
    half = thickness * 0.5
    bore = max(0.0, float(bore_radius or 0.0))

    verts = []
    for r, a in pts:
        verts.append((r * math.cos(a), r * math.sin(a), -half))
    for r, a in pts:
        verts.append((bore * math.cos(a), bore * math.sin(a), -half))
    for r, a in pts:
        verts.append((r * math.cos(a), r * math.sin(a), half))
    for r, a in pts:
        verts.append((bore * math.cos(a), bore * math.sin(a), half))

    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n + j, n + i])                 # 底面环
        faces.append([2 * n + i, 2 * n + j, 3 * n + j, 3 * n + i])  # 顶面环
        faces.append([i, j, 2 * n + j, 2 * n + i])         # 外齿壁
        faces.append([n + i, 3 * n + i, 3 * n + j, n + j])  # 孔壁

    obj = _mesh_from_pydata(name, verts, faces)
    _select_only(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")

    bevel = obj.modifiers.new(name="EdgeBevel", type="BEVEL")
    bevel.width = min(0.015, pitch_radius * 0.015)
    bevel.segments = 2
    bevel.limit_method = "ANGLE"
    bevel.angle_limit = math.radians(40)

    _auto_smoothfill(obj, 30)
    return obj


# --------------------------------------------------------------------------- #
# 六角螺栓
# --------------------------------------------------------------------------- #
def hex_bolt(shaft_radius=0.18, length=1.4, head_radius=0.42, head_height=0.28,
             name="Bolt"):
    """六角头螺栓：六角头 + 光杆 + 端部倒角。"""
    bpy.ops.mesh.primitive_cylinder_add(vertices=6, radius=head_radius,
                                        depth=head_height, location=(0, 0, head_height * 0.5))
    head = bpy.context.object
    head.name = f"{name}_Head"

    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=shaft_radius,
                                        depth=length, location=(0, 0, -length * 0.5))
    shaft = bpy.context.object
    shaft.name = f"{name}_Shaft"

    _select_only(head)
    shaft.select_set(True)
    bpy.ops.object.join()
    obj = bpy.context.object
    obj.name = name

    bevel = obj.modifiers.new(name="HeadBevel", type="BEVEL")
    bevel.width = min(head_height * 0.12, shaft_radius * 0.2)
    bevel.segments = 2
    bevel.limit_method = "ANGLE"
    bevel.angle_limit = math.radians(40)
    _auto_smoothfill(obj, 30)
    return obj


# --------------------------------------------------------------------------- #
# 旋转体花瓶（lathe）
# --------------------------------------------------------------------------- #
def _chaikin(points, iterations=3):
    pts = list(points)
    for _ in range(iterations):
        out = [pts[0]]
        for i in range(len(pts) - 1):
            p, q = pts[i], pts[i + 1]
            out.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            out.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        out.append(pts[-1])
        pts = out
    return pts


def vase(segments=64, height=2.0, thickness=0.05, name="Vase", profile=None):
    """旋转成型花瓶：先 lathe 外曲面，再用 Solidify 给壁厚（顶口/底口自动封边）。"""
    if profile is None:
        profile = [(0.30, 0.00), (0.46, 0.02), (0.52, 0.18), (0.60, 0.55),
                   (0.50, 1.05), (0.30, 1.55), (0.33, 1.90), (0.40, 2.00)]
    max_z = max(z for _, z in profile)
    base = [(r, z / max_z * height) for r, z in profile]
    pts = _chaikin(base, 3)
    n = len(pts)

    verts = []
    for k in range(segments):
        a = 2.0 * math.pi * k / segments
        c, s = math.cos(a), math.sin(a)
        for r, z in pts:
            verts.append((r * c, r * s, z))

    faces = []
    for k in range(segments):
        k2 = (k + 1) % segments
        for i in range(n - 1):
            faces.append([k * n + i, k2 * n + i, k2 * n + i + 1, k * n + i + 1])

    obj = _mesh_from_pydata(name, verts, faces)
    _select_only(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")

    solid = obj.modifiers.new(name="Wall", type="SOLIDIFY")
    solid.thickness = thickness
    solid.offset = -1.0
    solid.use_rim = True
    solid.use_rim_only = False

    bevel = obj.modifiers.new(name="EdgeSoft", type="BEVEL")
    bevel.width = thickness * 0.6
    bevel.segments = 2
    bevel.limit_method = "ANGLE"
    bevel.angle_limit = math.radians(50)
    _auto_smoothfill(obj, 40)
    return obj


# --------------------------------------------------------------------------- #
# 环面纽结（p, q）
# --------------------------------------------------------------------------- #
def torus_knot(p=2, q=3, major_radius=1.0, minor_radius=0.18, segments=240,
               name="TorusKnot"):
    """(p,q) 环面纽结，用曲线 bevel 生成。"""
    curve = bpy.data.curves.new(name, type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 12
    curve.bevel_depth = minor_radius
    curve.bevel_resolution = 6
    curve.use_fill_caps = True
    spline = curve.splines.new("POLY")
    spline.use_cyclic_u = True
    pts = []
    for i in range(segments):
        t = 2.0 * math.pi * i / segments
        r = major_radius + minor_radius * 0.0 + 0.55 * math.cos(q * t)
        x = r * math.cos(p * t)
        y = r * math.sin(p * t)
        z = 0.45 * math.sin(q * t)
        pts.append((x, y, z))
    spline.points.add(len(pts) - 1)
    for i, (x, y, z) in enumerate(pts):
        spline.points[i].co = (x, y, z, 1.0)
    obj = bpy.data.objects.new(name, curve)
    _link(obj)
    _select_only(obj)
    bpy.ops.object.convert(target="MESH")
    obj = bpy.context.object
    _auto_smoothfill(obj, 40)
    return obj


# --------------------------------------------------------------------------- #
# 低模树（示意用）
# --------------------------------------------------------------------------- #
def lowpoly_tree(trunk_height=1.6, trunk_radius=0.12, crown_radius=0.9,
                 seed=1, name="Tree"):
    import random
    rnd = random.Random(seed)
    bpy.ops.mesh.primitive_cone_add(vertices=7, radius1=trunk_radius, radius2=trunk_radius * 0.6,
                                    depth=trunk_height, location=(0, 0, trunk_height * 0.5))
    trunk = bpy.context.object
    trunk.name = f"{name}_Trunk"
    parts = [trunk]
    for i, z in enumerate([trunk_height * 0.85, trunk_height * 1.25, trunk_height * 1.65]):
        rad = crown_radius * (1.0 - i * 0.22)
        bpy.ops.mesh.primitive_cone_add(vertices=7, radius1=rad, radius2=0.05,
                                        depth=rad * 1.15, location=(0, 0, z),
                                        rotation=(0, 0, rnd.uniform(0, math.tau)))
        parts.append(bpy.context.object)
    _select_only(trunk)
    for o in parts[1:]:
        o.select_set(True)
    bpy.ops.object.join()
    obj = bpy.context.object
    obj.name = name
    return obj

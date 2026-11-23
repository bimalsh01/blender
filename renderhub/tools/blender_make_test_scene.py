"""Runs INSIDE Blender. Builds the tiny synthetic test scene and saves it.
Args JSON: {"blend": "<out .blend>", "missing_texture": false}
The checker texture is written to <package>/assets/textures/checker.png and referenced
with a relative path. With missing_texture=true, a second texture points at a file
that is never shipped (tests the Blender-side missing-texture check)."""

import json
import math
import os
import sys

import bpy

args = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
blend = os.path.abspath(args["blend"])
pkg = os.path.dirname(blend)
tex_dir = os.path.join(pkg, "assets", "textures")
os.makedirs(tex_dir, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.name = "Main"
try:
    scene.render.engine = "CYCLES"
except TypeError:
    pass
scene.cycles.samples = 16
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 216, 384, 100
scene.render.fps = 30
scene.frame_start, scene.frame_end = 1, 12

# Checker image saved next to the package, referenced relatively.
img = bpy.data.images.new("checker", 256, 256)
img.generated_type = "COLOR_GRID"
img.filepath_raw = os.path.join(tex_dir, "checker.png")
img.file_format = "PNG"
img.save()
img.source = "FILE"

world = bpy.data.worlds.new("World")
scene.world = world
world.use_nodes = True
bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
bg.inputs[0].default_value = (0.25, 0.35, 0.5, 1.0)

mat = bpy.data.materials.new("Checker")
mat.use_nodes = True
nt = mat.node_tree
bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
tex = nt.nodes.new("ShaderNodeTexImage")
tex.image = img
nt.links.new(tex.outputs[0], bsdf.inputs[0])
if args.get("missing_texture"):
    ghost = bpy.data.images.new("roughness_map", 8, 8)
    ghost.source = "FILE"
    ghost.filepath = "//assets/textures/roughness_map_not_shipped.png"
    t2 = nt.nodes.new("ShaderNodeTexImage")
    t2.image = ghost
    rough = next((i for i in bsdf.inputs if i.name == "Roughness"), None)
    if rough is not None:
        nt.links.new(t2.outputs[0], rough)

bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, 0))
bpy.ops.mesh.primitive_cube_add(size=1.5, location=(0, 0, 1))
cube = bpy.context.active_object
cube.name = "Cube"
cube.data.materials.append(mat)
cube.rotation_euler = (0, 0, 0)
cube.keyframe_insert("rotation_euler", frame=1)
cube.rotation_euler = (0, 0, math.radians(180))
cube.keyframe_insert("rotation_euler", frame=12)

bpy.ops.object.light_add(type="SUN", location=(3, -3, 6))
bpy.context.active_object.data.energy = 3.0
bpy.ops.object.camera_add(location=(0, -7, 3), rotation=(math.radians(72), 0, 0))
cam = bpy.context.active_object
cam.name = "Camera"
scene.camera = cam

bpy.ops.file.make_paths_relative()
bpy.ops.wm.save_as_mainfile(filepath=blend, relative_remap=True, compress=False)
img_count = sum(1 for i in bpy.data.images if i.source not in {"GENERATED", "VIEWER"})
print("RENDERHUB_JSON:" + json.dumps({"ok": True, "images": img_count, "libraries": len(bpy.data.libraries),
                                      "version": bpy.app.version_string}))

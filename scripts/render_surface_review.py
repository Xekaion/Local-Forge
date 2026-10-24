"""Native Blender CPU inspection of an exported LocalForge surface asset.

blender --background --factory-startup --python scripts/render_surface_review.py
  -- path/to/asset path/to/review
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def aim(obj, point):
    obj.rotation_euler = (
        (Vector(point) - obj.location).to_track_quat("-Z", "Y").to_euler()
    )


asset, review = (
    Path(value).resolve() for value in sys.argv[sys.argv.index("--") + 1 :]
)
review.mkdir(parents=True, exist_ok=False)
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.gltf(filepath=str(asset / "TimberPanel.glb"))
meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
if len(meshes) != 1:
    raise RuntimeError("Expected exactly one native glTF mesh.")
panel = meshes[0]
panel.location.z += 0.10
material = panel.data.materials[0]
principled = next(
    node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
)
if (
    not principled.inputs["Normal"].is_linked
    or not principled.inputs["Roughness"].is_linked
):
    raise RuntimeError("glTF PBR normal and roughness maps must remain connected.")
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 48
scene.cycles.use_denoising = True
scene.render.resolution_x, scene.render.resolution_y = 1200, 720
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.world.color = (0.055, 0.065, 0.08)
scene.view_settings.view_transform = "AgX"
scene.view_settings.look = "AgX - Medium High Contrast"
floor = bpy.data.materials.new("NeutralReviewFloor")
floor.diffuse_color = (0.11, 0.13, 0.16, 1)
floor.use_nodes = True
floor.node_tree.nodes.get("Principled BSDF").inputs["Roughness"].default_value = 0.82
bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, -0.003))
bpy.context.object.data.materials.append(floor)
for name, location, power, size, color in (
    ("LargeKey", (-0.55, -0.45, 1.2), 45, 0.75, (1.0, 0.91, 0.79)),
    ("CoolRim", (0.7, 0.35, 0.65), 25, 0.5, (0.69, 0.81, 1.0)),
):
    light = bpy.data.lights.new(name, type="AREA")
    light.energy, light.shape, light.size, light.color = power, "DISK", size, color
    obj = bpy.data.objects.new(name, light)
    scene.collection.objects.link(obj)
    obj.location = location
    aim(obj, (0, 0, 0.1))
camera = bpy.data.objects.new("ReviewCamera", bpy.data.cameras.new("ReviewCamera"))
scene.collection.objects.link(camera)
scene.camera = camera
camera.data.type, camera.data.ortho_scale = "ORTHO", 1.04
files = []
for name, location, target, scale in (
    ("PanelFront", (0.25, -0.9, 0.4), (0, 0, 0.1), 1.04),
    ("BevelEnd", (0.7, -0.48, 0.32), (0.30, 0, 0.1), 0.42),
    ("GrazingGrain", (-0.25, -0.8, 0.23), (-0.07, 0, 0.1), 0.52),
):
    camera.location = location
    camera.data.ortho_scale = scale
    aim(camera, target)
    path = review / f"{name}.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    files.append(
        {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "width": scene.render.resolution_x,
            "height": scene.render.resolution_y,
        }
    )
report = {
    "schema_version": 1,
    "engine": "Blender Cycles",
    "device": "CPU",
    "samples": 48,
    "blender": bpy.app.version_string,
    "input_glb_sha256": hashlib.sha256(
        (asset / "TimberPanel.glb").read_bytes()
    ).hexdigest(),
    "native_import_meshes": len(meshes),
    "vertices": len(panel.data.vertices),
    "polygons": len(panel.data.polygons),
    "normal_linked": True,
    "roughness_linked": True,
    "renders": files,
    "quality_approved": False,
    "paid_calls": 0,
}
(review / "render.json").write_text(
    json.dumps(report, indent=2) + "\n", encoding="utf-8"
)
print("LOCALFORGE_NATIVE_SURFACE_REVIEW=" + json.dumps(report))

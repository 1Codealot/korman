#    This file is part of Korman.
#
#    Korman is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Korman is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Korman.  If not, see <http://www.gnu.org/licenses/>.

import bpy
from bpy.app.handlers import persistent

from .logger import ExportProgressLogger
from .mesh import _VERTEX_COLOR_LAYERS
from ..helpers import *

_NUM_RENDER_LAYERS = 20

class LightBaker:
    """ExportTime Lighting"""

    def __init__(self, report=None):
        self._lightgroups = {}
        if report is None:
            self._report = ExportProgressLogger()
            self.add_progress_steps(self._report)
            self._report.progress_start("PREVIEWING LIGHTING")
            self._own_report = True
        else:
            self._report = report
            self._own_report = False
        self._uvtexs = {}

    def __del__(self):
        if self._own_report:
            self._report.progress_end()

    @staticmethod
    def add_progress_steps(report):
        report.progress_add_step("Searching for Bahro")
        report.progress_add_step("Baking Static Lighting")

    def _apply_render_settings(self, toggle, vcols):
        render = bpy.context.scene.render
        toggle.track(render, "use_textures", False)
        toggle.track(render, "use_shadows", True)
        toggle.track(render, "use_envmaps", False)
        toggle.track(render, "use_raytrace", True)
        toggle.track(render, "bake_type", "FULL")
        toggle.track(render, "use_bake_clear", True)
        toggle.track(render, "use_bake_to_vertex_color", vcols)

    def _associate_image_with_uvtex(self, uvtex, im):
        # Associate the image with all the new UVs
        # NOTE: no toggle here because it's the artist's problem if they are looking at our
        #       super swagalicious LIGHTMAPGEN uvtexture...
        for i in uvtex.data:
            i.image = im

    def _bake_lightmaps(self, objs, layers):
        with GoodNeighbor() as toggle:
            scene = bpy.context.scene
            scene.layers = layers
            self._apply_render_settings(toggle, False)
            self._select_only(objs, toggle)
            bpy.ops.object.bake_image()

    def _bake_vcols(self, objs):
        with GoodNeighbor() as toggle:
            bpy.context.scene.layers = (True,) * _NUM_RENDER_LAYERS
            self._apply_render_settings(toggle, True)
            self._select_only(objs, toggle)
            bpy.ops.object.bake_image()

    def bake_static_lighting(self, objs):
        """Bakes all static lighting for Plasma geometry"""

        self._report.msg("\nBaking Static Lighting...")
        bake = self._harvest_bakable_objects(objs)

        with GoodNeighbor() as toggle:
            try:
                # reduce the amount of indentation
                result = self._bake_static_lighting(bake, toggle)
            finally:
                # this stuff has been observed to be problematic with GoodNeighbor
                self._pop_lightgroups()
                self._restore_uvtexs()
            return result

    def _bake_static_lighting(self, bake, toggle):
        inc_progress = self._report.progress_increment

        # Step 0.9: Make all layers visible.
        #           This prevents context operators from phailing.
        bpy.context.scene.layers = (True,) * _NUM_RENDER_LAYERS

        # Step 1: Prepare... Apply UVs, etc, etc, etc
        self._report.progress_advance()
        self._report.progress_range = len(bake)
        self._report.msg("Preparing to bake...", indent=1)
        for key in bake.keys():
            if key[0] == "lightmap":
                for i in range(len(bake[key])-1, -1, -1):
                    obj = bake[key][i]
                    if not self._prep_for_lightmap(obj, toggle):
                        self._report.msg("Lightmap '{}' will not be baked -- no applicable lights",
                                         obj.name, indent=2)
                        bake[key].pop(i)
            elif key[0] == "vcol":
                for i in range(len(bake[key])-1, -1, -1):
                    obj = bake[key][i]
                    if not self._prep_for_vcols(obj, toggle):
                        if self._has_valid_material(obj):
                            self._report.msg("VCols '{}' will not be baked -- no applicable lights",
                                             obj.name, indent=2)
                        bake[key].pop(i)
            else:
                raise RuntimeError(key[0])
            inc_progress()
        self._report.msg("    ...")

        # Step 2: BAKE!
        self._report.progress_advance()
        self._report.progress_range = len(bake)
        for key, value in bake.items():
            if value:
                if key[0] == "lightmap":
                    self._report.msg("{} Lightmap(s) [H:{:X}]", len(value), hash(key), indent=1)
                    self._bake_lightmaps(value, key[1:])
                elif key[0] == "vcol":
                    self._report.msg("{} Crap Light(s)", len(value), indent=1)
                    self._bake_vcols(value)
                else:
                    raise RuntimeError(key[0])
            inc_progress()

        # Return how many thingos we baked
        return sum(map(len, bake.values()))

    def _generate_lightgroup(self, bo, user_lg=None):
        """Makes a new light group for the baking process that excludes all Plasma RT lamps"""

        if user_lg is not None:
            user_lg = bpy.data.groups.get(user_lg)
        shouldibake = (user_lg is not None and bool(user_lg.objects))

        mesh = bo.data
        for material in mesh.materials:
            if material is None:
                # material is not assigned to this material... (why is this even a thing?)
                continue

            # Already done it?
            lg, mat_name = material.light_group, material.name
            if mat_name not in self._lightgroups:
                self._lightgroups[mat_name] = lg

            if user_lg is None:
                if not lg or bool(lg.objects) is False:
                    source = [i for i in bpy.data.objects if i.type == "LAMP"]
                else:
                    source = lg.objects
                dest = bpy.data.groups.new("_LIGHTMAPGEN_{}_{}".format(bo.name, mat_name))

                # Rules:
                # 1) No animated lights, period.
                # 2) If we accept runtime lighting, no Plasma Objects
                rtl_mod = bo.plasma_modifiers.lighting
                for obj in source:
                    if obj.plasma_object.has_animation_data:
                        continue
                    if rtl_mod.rt_lights and obj.plasma_object.enabled:
                        continue
                    dest.objects.link(obj)
                    shouldibake = True
            else:
                # The aforementioned rules do not apply. You better hope you know WTF you are
                # doing. I'm not going to help!
                dest = user_lg
            material.light_group = dest
        return shouldibake

    def _get_lightmap_uvtex(self, mesh, modifier):
        if modifier.uv_map:
            return mesh.uv_textures[modifier.uv_map]
        for i in mesh.uv_textures:
            if i.name != "LIGHTMAPGEN":
                return i
        return None

    def _has_valid_material(self, bo):
        for material in bo.data.materials:
            if material is not None:
                return True
        return False

    def _harvest_bakable_objects(self, objs):
        # The goal here is to minimize the calls to bake_image, so we are going to collect everything
        # that needs to be baked and sort it out by configuration.
        bake = { ("vcol",): [] }
        for i in objs:
            if i.type != "MESH":
                continue
            if bool(i.data.materials) is False:
                continue

            mods = i.plasma_modifiers
            if mods.lightmap.enabled:
                key = ("lightmap",) + tuple(mods.lightmap.render_layers)
                if key in bake:
                    bake[key].append(i)
                else:
                    bake[key] = [i,]
            elif mods.lighting.preshade:
                vcols = i.data.vertex_colors
                for j in _VERTEX_COLOR_LAYERS:
                    if j in vcols:
                        break
                else:
                    bake[("vcol",)].append(i)
        return bake

    def _pop_lightgroups(self):
        materials = bpy.data.materials
        for mat_name, lg in self._lightgroups.items():
            materials[mat_name].light_group = lg
        self._lightgroups.clear()

        groups = bpy.data.groups
        for i in groups:
            if i.name.startswith("_LIGHTMAPGEN_"):
                i.user_clear()
                bpy.data.groups.remove(i)

    def _prep_for_lightmap(self, bo, toggle):
        mesh = bo.data
        modifier = bo.plasma_modifiers.lightmap
        uv_textures = mesh.uv_textures

        # Create a special light group for baking
        if not self._generate_lightgroup(bo, modifier.light_group):
            return False

        # We need to ensure that we bake onto the "BlahObject_LIGHTMAPGEN" image
        data_images = bpy.data.images
        im_name = "{}_LIGHTMAPGEN.png".format(bo.name)
        size = modifier.resolution

        im = data_images.get(im_name)
        if im is None:
            im = data_images.new(im_name, width=size, height=size)
        elif im.size[0] != size:
            # Force delete and recreate the image because the size is out of date
            im.user_clear()
            data_images.remove(im)
            im = data_images.new(im_name, width=size, height=size)

        # If there is a cached LIGHTMAPGEN uvtexture, nuke it
        uvtex = uv_textures.get("LIGHTMAPGEN", None)
        if uvtex is not None:
            uv_textures.remove(uvtex)

        # Make sure we can enter Edit Mode(TM)
        toggle.track(bo, "hide", False)

        # Because the way Blender tracks active UV layers is massively stupid...
        self._uvtexs[mesh.name] = uv_textures.active.name

        # We must make this the active object before touching any operators
        bpy.context.scene.objects.active = bo

        # Originally, we used the lightmap unpack UV operator to make our UV texture, however,
        # this tended to create sharp edges. There was already a discussion about this on the
        # Guild of Writers forum, so I'm implementing a code version of dendwaler's process,
        # as detailed here: https://forum.guildofwriters.org/viewtopic.php?p=62572#p62572
        uv_base = self._get_lightmap_uvtex(mesh, modifier)
        if uv_base is not None:
            uv_textures.active = uv_base
            # this will copy the UVs to the new UV texture
            uvtex = uv_textures.new("LIGHTMAPGEN")
            uv_textures.active = uvtex
            self._associate_image_with_uvtex(uvtex, im)
            # here we go...
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.average_islands_scale()
            bpy.ops.uv.pack_islands()
        else:
            # same thread, see Sirius's suggestion RE smart unwrap. this seems to yield good
            # results in my tests. it will be good enough for quick exports.
            uvtex = uv_textures.new("LIGHTMAPGEN")
            self._associate_image_with_uvtex(uvtex, im)
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project()
        bpy.ops.object.mode_set(mode="OBJECT")

        # Now, set the new LIGHTMAPGEN uv layer as what we want to render to...
        # NOTE that this will need to be reset by us to what the user had previously
        # Not using toggle.track due to observed oddities
        for i in uv_textures:
            value = i.name == "LIGHTMAPGEN"
            i.active = value
            i.active_render = value

        # Indicate we should bake
        return True

    def _prep_for_vcols(self, bo, toggle):
        mesh = bo.data
        vcols = mesh.vertex_colors

        # Create a special light group for baking
        if not self._generate_lightgroup(bo):
            return False

        # I have heard tale of some moar "No valid image to bake to" boogs if there is a really
        # old copy of the autocolor layer on the mesh. Nuke it.
        autocolor = vcols.get("autocolor")
        if autocolor is not None:
            vcols.remove(autocolor)
        autocolor = vcols.new("autocolor")
        toggle.track(vcols, "active", autocolor)

        # Mark "autocolor" as our active render layer
        for vcol_layer in mesh.vertex_colors:
            autocol = vcol_layer.name == "autocolor"
            toggle.track(vcol_layer, "active_render", autocol)
            toggle.track(vcol_layer, "active", autocol)
        mesh.update()

        # Indicate we should bake
        return True

    def _restore_uvtexs(self):
        for mesh_name, uvtex_name in self._uvtexs.items():
            mesh = bpy.data.meshes[mesh_name]
            for i in mesh.uv_textures:
                i.active = uvtex_name == i.name
            mesh.uv_textures.active = mesh.uv_textures[uvtex_name]

    def _select_only(self, objs, toggle):
        if isinstance(objs, bpy.types.Object):
            toggle.track(objs, "hide_render", False)
            for i in bpy.data.objects:
                i.select = i == objs
                if isinstance(i.data, bpy.types.Mesh) and not self._has_valid_material(i):
                    toggle.track(i, "hide_render", True)
        else:
            for i in bpy.data.objects:
                value = i in objs
                if value:
                    toggle.track(i, "hide_render", False)
                elif isinstance(i.data, bpy.types.Mesh) and not self._has_valid_material(i):
                    toggle.track(i, "hide_render", True)
                i.select = value

@persistent
def _toss_garbage(scene):
    """Removes all LIGHTMAPGEN and autocolor garbage before saving"""
    for i in bpy.data.images:
        if i.name.endswith("_LIGHTMAPGEN.png"):
            i.user_clear()
            bpy.data.images.remove(i)
    for i in bpy.data.meshes:
        for uv_tex in i.uv_textures:
            if uv_tex.name == "LIGHTMAPGEN":
                i.uv_textures.remove(uv_tex)
        for vcol in i.vertex_colors:
            if vcol.name == "autocolor":
                i.vertex_colors.remove(vcol)

# collects light baking garbage
bpy.app.handlers.save_pre.append(_toss_garbage)

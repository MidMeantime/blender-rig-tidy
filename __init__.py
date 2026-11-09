bl_info = {
    "name": "Rig Tidy",
    "author": "MidMeantime",
    "version": (0, 8, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Rig Tidy",
    "description": "Cleans up imported Biped / game rigs: bone directions & rolls, swapped L/R names, "
                   "chain naming, dictionary translation and bone collections",
    "category": "Rigging",
}

# Files in this folder:
#   rig_tidy.py      - the add-on itself
#   dictionary.csv   - word,english pairs used to translate bone names (edit freely)

if "rig_tidy" in locals():
    import importlib
    importlib.reload(rig_tidy)  # noqa: F821  (F8 / script reload)
else:
    from . import rig_tidy


def register():
    rig_tidy.register()


def unregister():
    rig_tidy.unregister()

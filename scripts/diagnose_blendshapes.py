# diagnose_blendshapes.py - READ-ONLY blendshape wiring diagnostic.
# Paste the whole file into Maya's Script Editor (Python tab) and run.
# It changes nothing in the scene; it only prints how the blendShape
# deformer is wired so we can fix the USD export.
import maya.cmds as cmds
BS = "Mesh_ncl1_1"
grps = cmds.ls("*Blendshapes*", long=True, type="transform") or []
print("Blendshapes group candidates:", grps)
grp = grps[0] if grps else None
if grp:
    kids = cmds.listRelatives(grp, children=True, fullPath=True) or []
    print("child count:", len(kids), "| first few:", [k.split("|")[-1] for k in kids[:5]])
aliases = cmds.aliasAttr(BS, q=True) or []
idx_by_alias = {a: int(attr[7:-1]) for a, attr in zip(aliases[0::2], aliases[1::2]) if attr.startswith("weight[")}
print("deformer target count:", len(idx_by_alias))
for alias in list(idx_by_alias)[:3]:
    i = idx_by_alias[alias]
    item = "%s.inputTarget[0].inputTargetGroup[%d].inputTargetItem[6000]" % (BS, i)
    geo = cmds.listConnections(item + ".inputGeomTarget", s=True, d=False) or []
    try:
        has_pts = bool(cmds.getAttr(item + ".inputPointsTarget"))
    except Exception:
        has_pts = False
    print("[%s] idx=%d connected_geo=%s baked_deltas=%s" % (alias, i, geo, has_pts))

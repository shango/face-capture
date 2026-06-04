"""export_arkit_head_to_usd.py - One-off export of the ARKit head .ma to USD.

Run this ONCE locally in Maya (any version >= 2022 with the mayaUsdPlugin).
Output: pipeline/assets/arkit_head.usdc — committed to the repo and bundled
with every download. The per-job pipeline.usd_export.bake_animated_usd()
references this file by relative path; never re-export it per job.

Why this lives in scripts/ and not the pipeline package:
  - It depends on `maya.cmds` and `mayaUSDExport`, which only exist inside a
    Maya install. Importing it server-side would crash on `import maya.cmds`.
  - It runs once per rig change, not once per job.

Why we reset blendshape weights to 0 before exporting:
  - The supplied AR_kit_Gead_Geo.ma was saved with non-zero default weights
    (the rig was last viewed in a pose). mayaUSDExport bakes the *current*
    rest pose into the exported neutral mesh, so without the reset the
    static USD comes out deformed and every animation starts from that
    pose plus the CSV's delta. Force-zero gives a true neutral.

Two ways to run:

  (A) From a terminal with mayapy on PATH:
        mayapy scripts/export_arkit_head_to_usd.py \\
            --ma  AR_kit_Gead_Geo.ma \\
            --out pipeline/assets/arkit_head.usdc

  (B) From inside Maya — open the Script Editor (Python tab), paste:
        import sys
        sys.path.insert(0, r"C:/path/to/face-capture/scripts")
        import export_arkit_head_to_usd as exp
        exp.export(
            ma_path  = r"C:/path/to/face-capture/AR_kit_Gead_Geo.ma",
            out_path = r"C:/path/to/face-capture/pipeline/assets/arkit_head.usdc",
        )

Either way the script:
  1. Opens the .ma (force-new scene, no save prompts).
  2. Locates the blendShape deformer and the deformed mesh.
  3. Verifies the deformer's target aliases against the ARKit-52 standard;
     warns about anything missing or unexpected (the supplied rig has 51 of
     52 — tongueOut is normal to omit and will just be skipped at bake
     time).
  4. Zeroes every blendshape weight, so the exported neutral mesh is the
     true rest shape.
  5. Selects the top-level head transform and runs mayaUSDExport with
     -exportBlendShapes 1 -defaultUSDFormat usdc -selection 1.
  6. Reports the output path and target count.

If the deformer node name in your .ma differs from "Mesh_ncl1_1", pass
--blendshape-node "<your name>" (or set blendshape_node= when calling
export()).
"""

from __future__ import annotations

import argparse
import os
import sys


# The 52 standard ARKit blendshape names. The supplied rig has 51 — tongueOut
# is intentionally omitted by Apple's reference and most face rigs follow.
ARKIT_52 = (
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft",
    "eyeLookUpLeft", "eyeSquintLeft", "eyeWideLeft",
    "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight",
    "eyeLookUpRight", "eyeSquintRight", "eyeWideRight",
    "jawForward", "jawLeft", "jawRight", "jawOpen",
    "mouthClose", "mouthFunnel", "mouthPucker", "mouthRight", "mouthLeft",
    "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
    "mouthDimpleLeft", "mouthDimpleRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper",
    "mouthPressLeft", "mouthPressRight",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "noseSneerLeft", "noseSneerRight",
    "tongueOut",
)


def _import_maya():
    """Import maya.cmds lazily so this module can be syntax-checked outside Maya."""
    try:
        import maya.cmds as cmds  # type: ignore
        import maya.standalone  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "This script must run inside Maya or under mayapy. "
            "Could not import maya.cmds: %s" % exc
        ) from exc
    return cmds, maya.standalone


def _ensure_standalone_initialized(standalone) -> bool:
    """Initialize maya.standalone if we're running under mayapy (not inside Maya).

    Returns True if we initialized it (and therefore should uninitialize on
    exit), False if we were already inside Maya.
    """
    try:
        # `standalone.initialize()` is a no-op + harmless inside Maya itself,
        # but returns None either way. We use a sentinel attribute to tell.
        # Simpler: just attempt it; mayapy needs it, Maya GUI tolerates it.
        standalone.initialize(name="python")
        return True
    except RuntimeError:
        # Already initialized.
        return False


def _list_blendshape_targets(cmds, node: str) -> dict[str, int]:
    """Return {alias_name: weight_index} for every target on `node`."""
    aliases = cmds.aliasAttr(node, query=True) or []
    out: dict[str, int] = {}
    for alias, attr in zip(aliases[0::2], aliases[1::2]):
        if attr.startswith("weight["):
            out[alias] = int(attr[len("weight["):-1])
    return out


def _zero_all_weights(cmds, node: str, targets: dict[str, int]) -> None:
    """Force every blendshape weight to 0 so the export captures the neutral mesh."""
    for name, idx in targets.items():
        attr = "%s.weight[%d]" % (node, idx)
        try:
            cmds.setAttr(attr, 0.0)
        except RuntimeError as exc:
            # Locked or driven — try to break the connection then retry.
            try:
                cmds.setAttr(attr, lock=False)
                cmds.setAttr(attr, 0.0)
            except RuntimeError:
                print(
                    "[export] WARNING: could not zero %s (%s) — exported neutral "
                    "may carry this pose offset." % (attr, exc),
                    file=sys.stderr,
                )


def _find_deformed_mesh(cmds, blendshape_node: str) -> str:
    """Return the dag path of the mesh shape the blendShape node deforms.

    The blendShape's `.outputGeometry[0]` is wired to the downstream shape;
    we walk that connection. Falls back to the first geometry filter set
    member if needed.
    """
    downstream = cmds.listConnections(
        "%s.outputGeometry[0]" % blendshape_node,
        source=False,
        destination=True,
        shapes=True,
    ) or []
    if downstream:
        return downstream[0]
    members = cmds.ls(cmds.listHistory("%s" % blendshape_node, future=True), type="mesh")
    if not members:
        raise RuntimeError(
            "Could not determine the deformed mesh for %s — check the rig."
            % blendshape_node
        )
    return members[0]


def export(
    ma_path: str,
    out_path: str,
    blendshape_node: str = "Mesh_ncl1_1",
) -> None:
    """Export `ma_path` to a USD at `out_path` with blendshape targets baked in."""
    cmds, standalone = _import_maya()
    _ensure_standalone_initialized(standalone)

    # mayaUsdPlugin ships with Maya 2022+ but isn't auto-loaded in mayapy.
    if not cmds.pluginInfo("mayaUsdPlugin", query=True, loaded=True):
        cmds.loadPlugin("mayaUsdPlugin")

    ma_path = os.path.abspath(ma_path)
    out_path = os.path.abspath(out_path)
    if not os.path.isfile(ma_path):
        raise FileNotFoundError(ma_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print("[export] opening %s" % ma_path)
    cmds.file(ma_path, open=True, force=True, prompt=False, ignoreVersion=True)

    if not cmds.objExists(blendshape_node):
        raise RuntimeError(
            "blendShape node %r not found. Pass --blendshape-node to point at "
            "the correct one." % blendshape_node
        )

    targets = _list_blendshape_targets(cmds, blendshape_node)
    print("[export] %s has %d target aliases" % (blendshape_node, len(targets)))

    missing = [n for n in ARKIT_52 if n not in targets]
    extra = sorted(set(targets) - set(ARKIT_52))
    if missing:
        print("[export] note: ARKit shapes absent from rig (will be skipped at "
              "bake time): %s" % ", ".join(missing))
    if extra:
        print("[export] note: non-ARKit targets present (kept as-is): %s"
              % ", ".join(extra))

    _zero_all_weights(cmds, blendshape_node, targets)

    mesh_shape = _find_deformed_mesh(cmds, blendshape_node)
    mesh_xform = cmds.listRelatives(mesh_shape, parent=True, fullPath=True)[0]
    # Select ONLY the deformed base mesh — never the hierarchy root.
    #
    # mayaUSDExport reads each blendshape target's geometry from the deformer
    # itself (via inputGeomTarget / baked deltas), so the target meshes must
    # NOT be in the selection. The supplied rig keeps its 51 targets live in a
    # sibling `Blendshapes` group (64 meshes); selecting the root dragged that
    # whole group into the export, and mayaUSD then aborts with
    # "Blendshapes were requested to be exported for <target>, but none could
    # be found" on each target mesh. Selecting just the base mesh exports its
    # ancestors as plain Xforms (grouping preserved) while leaving the target
    # group out, which is exactly what -exportBlendShapes expects.
    print("[export] deformed mesh: %s   shape: %s" % (mesh_xform, mesh_shape))

    cmds.select(mesh_xform, replace=True)

    print("[export] writing %s" % out_path)
    cmds.mayaUSDExport(
        file=out_path,
        selection=True,
        exportBlendShapes=True,
        # blendShapes are a UsdSkel feature: mayaUSD only writes BlendShape
        # prims + UsdSkelBindingAPI (skel:blendShapes) when exportSkels is
        # "auto". With "none" the whole UsdSkel schema is skipped and
        # -exportBlendShapes has nothing to attach to, producing an empty
        # stage. "auto" gives a SkelRoot + binding even though this rig has
        # no joints — which is exactly what pipeline/usd_export.py scans for.
        exportSkels="auto",
        exportUVs=True,
        exportColorSets=False,
        defaultUSDFormat="usdc",
        # `auto` writes one USD stage flat; `Single Layer File` semantics.
        mergeTransformAndShape=True,
        stripNamespaces=True,
    )
    print("[export] done. %d ARKit shapes available in %s"
          % (len(targets), out_path))


def _in_maya_gui() -> bool:
    """True inside an interactive Maya session (Script Editor), False under
    mayapy/batch or a plain interpreter."""
    try:
        import maya.cmds as cmds  # type: ignore
        return not cmds.about(batch=True)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ma", required=True, help="Path to AR_kit_Gead_Geo.ma")
    p.add_argument("--out", required=True,
                   help="Output .usdc path (e.g. pipeline/assets/arkit_head.usdc)")
    p.add_argument("--blendshape-node", default="Mesh_ncl1_1",
                   help="Name of the blendShape deformer (default: Mesh_ncl1_1)")
    args = p.parse_args(argv)
    export(ma_path=args.ma, out_path=args.out, blendshape_node=args.blendshape_node)
    return 0


if __name__ == "__main__":
    # Executing the whole file in Maya's Script Editor lands here with
    # sys.argv set to Maya's own launch args (no --ma/--out), so argparse
    # would abort with `SystemExit: 2`. Detect that and print the
    # import-and-call recipe instead of crashing.
    if _in_maya_gui() and not any(a in ("--ma", "--out") for a in sys.argv[1:]):
        print(
            "[export] Don't execute this file in the Script Editor — import it\n"
            "         and call export(). Paste this instead:\n\n"
            "    import sys, importlib\n"
            "    sys.path.insert(0, r'<repo>\\scripts')\n"
            "    import export_arkit_head_to_usd as exp\n"
            "    importlib.reload(exp)\n"
            "    exp.export(\n"
            "        ma_path  = r'<repo>\\AR_kit_Gead_Geo.ma',\n"
            "        out_path = r'<repo>\\pipeline\\assets\\arkit_head.usdc',\n"
            "    )\n"
        )
    else:
        raise SystemExit(main())

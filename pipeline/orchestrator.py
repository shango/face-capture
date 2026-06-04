"""
pipeline.py - End-to-end pipeline orchestrator.

Takes a source video and produces the user delivery bundle:
    <stem>_preview.mp4       (mesh overlay on the original source)
    <stem>_blendshapes.csv   (resampled ARKit-52, ready for any rig)
    <stem>_apply_in_maya.py  (Maya import script, CSV path baked in)
    <stem>_apply_in_blender.py (Blender import script, CSV path baked in)
    <stem>_README.txt        (usage instructions)

  If pipeline/assets/arkit_head.usdc is present (one-off Maya export
  via scripts/export_arkit_head_to_usd.py), two extra files are added:
    <stem>_arkit_head.usdc   (static neutral head with blendshape targets)
    <stem>_animated.usda     (head + per-frame blendshape weights from CSV)
  Open the .usda in any USD-capable DCC to scrub the animation directly.

All files are written into a single output directory (or zipped if
--zip is passed). Intermediate files are kept in a temp dir and cleaned
up unless --keep-intermediate is set.

Designed to be called either from the CLI or as a function from a web
service. The `run_pipeline()` function returns a dict describing where
each output file landed.

Usage:
    # CLI
    python pipeline.py source.mp4 -o ./jobs/job123 [--zip] [--keep-intermediate]

    # Python
    from pipeline import run_pipeline
    result = run_pipeline(Path("source.mp4"), Path("./jobs/job123"))
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).parent


def _safe_stem(name: str) -> str:
    """Filesystem/zip-safe slug derived from a clip filename stem.

    Callers pass an already-extensionless stem (Path.stem). Keeps only
    [A-Za-z0-9_-]; collapses any other run (incl. dots/spaces) into
    "_"; trims separators; caps length. Idempotent. Falls back to
    "clip" when nothing usable remains.
    """
    base = os.path.basename(name or "")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_-")
    slug = slug[:50].strip("_-")
    return slug or "clip"


def _run(cmd: list[str], description: str) -> None:
    print(f"\n=== {description} ===")
    print("  $ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Step failed: {description}")


def _detect_source_fps(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        if "/" in out:
            num, den = out.split("/", 1)
            d = float(den)
            return float(num) / d if d else 24.0
        return float(out)
    except (ValueError, ZeroDivisionError):
        # Malformed/empty ffprobe output (e.g. VFR streams) — fall back
        # to the nominal source rate rather than aborting the pipeline.
        return 24.0


def _write_apply_script(
    out_dir: Path,
    csv_filename: str,
    *,
    script_name: str = "apply_in_maya.py",
    readme_name: str = "README.txt",
) -> Path:
    """Write a Maya import script with CSV path baked in for end users."""
    target = out_dir / script_name
    target.write_text(f'''"""
{script_name} - Apply captured facial animation to your ARKit-52 mesh.

USAGE:

    1. Open your Maya scene containing the ARKit-rigged head.
    2. Run this script in Maya's Script Editor (Python tab).

If you're not sure how to do step 2, see {readme_name} in this bundle
-- it walks through opening the Script Editor and running this script
with no scripting experience assumed.

That's it. The script finds every blendShape node in the scene on its
own, sets the scene fps, and keys the captured weights onto matching
ARKit-named targets. It also locates the CSV automatically; if it
can't (e.g. you pasted this into the console rather than opening the
file), a dialog asks you to pick blendshapes.csv from the unzipped
bundle once. No discovery commands and no editing are required.

Columns that don't match a target on a given node are skipped silently.
If nothing matches anywhere, the script says so loudly -- this bundle
expects an ARKit-standard rig.
"""

import csv
import os
import maya.cmds as cmds


# ---- USER CONFIG ------------------------------------------------------

# The CSV ships next to this script and is found automatically. You
# only need to set CSV_PATH if you want to point at a CSV elsewhere;
# otherwise leave it as "" and the script locates {csv_filename!r}
# (it sits beside this file, or it will ask you to pick it).
CSV_NAME = {csv_filename!r}
CSV_PATH = ""

# Leave this EMPTY to auto-detect every blendShape node in the scene
# (the normal case -- handles split ARKit rigs with head/eyes/teeth
# nodes automatically). Only fill it in to restrict application to
# specific nodes in an unusual scene, e.g.:
#     BLENDSHAPE_NODES = ["head_blendShape", "eyes_blendShape"]
BLENDSHAPE_NODES = []  # type: list

# Set fps to match the CSV; 24 is the source rate.
SCENE_FPS = "film"  # 'film'=24, 'pal'=25, 'ntsc'=30, 'show'=48, 'palf'=50, 'ntscf'=60

# Erase any existing animation on matched attrs before re-keying.
CLEAR_EXISTING = True

# ---- END USER CONFIG --------------------------------------------------


POSE_COLS = ["head_yaw", "head_pitch", "head_roll",
             "head_tx", "head_ty", "head_tz"]
NON_TARGET_COLS = {{"frame", "time_seconds", "detected"}} | set(POSE_COLS)


def _list_aliases(node):
    aliases = cmds.aliasAttr(node, query=True) or []
    result = {{}}
    for alias, attr in zip(aliases[0::2], aliases[1::2]):
        if attr.startswith("weight["):
            idx = int(attr[len("weight["):-1])
            result[alias] = idx
    return result


# The 52 standard ARKit blendshape names. Used to recover a clean ARKit
# identity from rig targets that are namespaced (e.g. Face:jawOpen) or were
# mangled by a USD round-trip (e.g. _Mesh_ncl1_1_jawOpen_jawOpenShape). The
# CSV columns use clean ARKit names, so without this such a rig matches zero
# targets and you'd see the "no ARKit-52 targets matched" warning.
_ARKIT_52 = (
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft",
    "eyeLookUpLeft", "eyeSquintLeft", "eyeWideLeft",
    "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight",
    "eyeLookUpRight", "eyeSquintRight", "eyeWideRight",
    "jawForward", "jawLeft", "jawRight", "jawOpen",
    "mouthClose", "mouthFunnel", "mouthPucker", "mouthRight", "mouthLeft",
    "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
    "mouthDimpleLeft", "mouthDimpleRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthPressLeft", "mouthPressRight",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "noseSneerLeft", "noseSneerRight", "tongueOut",
)
_ARKIT_BY_LOWER = {{n.lower(): n for n in _ARKIT_52}}


def _canonical_arkit_name(rig_name):
    # Recover the ARKit name a (possibly namespaced/mangled) rig name encodes.
    # Drop a trailing "Shape", then take the longest ARKit name the result
    # ends with, on a name boundary (start or non-alphanumeric separator) so
    # an embedded substring can't false-match (mouthRight vs mouthStretchRight).
    low = rig_name.lower()
    if low.endswith("shape"):
        low = low[:-len("shape")]
    best = None
    for arkit_lower, arkit in _ARKIT_BY_LOWER.items():
        if not low.endswith(arkit_lower):
            continue
        boundary = len(low) - len(arkit_lower)
        if boundary != 0 and low[boundary - 1].isalnum():
            continue
        if best is None or len(arkit_lower) > len(best.lower()):
            best = arkit
    return best


def _match_columns(target_cols, rig_names):
    # Pair each clean-ARKit CSV column with the rig target that encodes it.
    # Case-insensitive direct match first; canonical ARKit recovery as a
    # fallback. Returns [(csv_col, rig_name)]; rig_name is what we key onto.
    by_key = {{}}
    for rn in rig_names:
        by_key.setdefault(rn.lower(), rn)
        canon = _canonical_arkit_name(rn)
        if canon is not None:
            by_key.setdefault(canon.lower(), rn)
    pairs = []
    for c in target_cols:
        rn = by_key.get(c.lower())
        if rn is not None:
            pairs.append((c, rn))
    return pairs


def _resolve_csv():
    # 1. Explicit override wins.
    if CSV_PATH and os.path.isfile(CSV_PATH):
        return CSV_PATH
    # 2. Next to this script -- works when run as a saved file
    #    (File > Open Script... then Execute, or execfile).
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        cand = os.path.join(here, CSV_NAME)
        if os.path.isfile(cand):
            return cand
    except NameError:
        # __file__ is undefined when the script is pasted into the
        # Script Editor and run from the console -- fall through.
        pass
    # 3. Current working directory.
    cand = os.path.abspath(CSV_NAME)
    if os.path.isfile(cand):
        return cand
    # 4. Ask the user to locate it -- always works, however the
    #    script was launched.
    sel = cmds.fileDialog2(
        fileMode=1, dialogStyle=2,
        caption="Select %s from the unzipped bundle" % CSV_NAME,
        fileFilter="CSV files (*.csv);;All files (*.*)")
    if sel:
        return sel[0]
    return None


def main():
    csv_path = _resolve_csv()
    if not csv_path:
        cmds.error("Could not locate %s. Set CSV_PATH at the top of "
                   "this script to its full path." % CSV_NAME)
        return

    # Set scene fps
    cmds.currentUnit(time=SCENE_FPS)
    fps_map = {{"game": 15, "film": 24, "pal": 25, "ntsc": 30,
                "show": 48, "palf": 50, "ntscf": 60}}
    fps = fps_map.get(SCENE_FPS, 24)

    print("[apply] using CSV: %s" % csv_path)
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames or []

    if not rows:
        cmds.error("CSV is empty.")
        return

    target_cols = [c for c in header if c not in NON_TARGET_COLS]
    print("[apply] %d data columns in CSV" % len(target_cols))
    print("[apply] %d frames" % len(rows))

    # Set timeline range
    last_t = float(rows[-1]["time_seconds"])
    last_frame = int(last_t * fps)
    cmds.playbackOptions(min=0, max=last_frame,
                         animationStartTime=0, animationEndTime=last_frame)

    nodes = BLENDSHAPE_NODES or (cmds.ls(type="blendShape") or [])
    if not nodes:
        cmds.error("No blendShape nodes found in the scene. Open your "
                   "ARKit-rigged scene before running this script.")
        return
    print("[apply] applying to %d blendShape node(s): %s"
          % (len(nodes), ", ".join(nodes)))

    total_keys = 0
    for node_name in nodes:
        if not cmds.objExists(node_name):
            print("[apply] WARNING: node not found, skipping: %s" % node_name)
            continue
        targets = _list_aliases(node_name)
        pairs = _match_columns(target_cols, list(targets.keys()))
        print("[apply] %s: matched %d/%d targets"
              % (node_name, len(pairs), len(targets)))

        if CLEAR_EXISTING:
            for col, alias in pairs:
                try:
                    cmds.cutKey("%s.%s" % (node_name, alias), clear=True)
                except Exception:
                    pass

        for row in rows:
            try:
                t = float(row["time_seconds"])
            except (ValueError, KeyError):
                continue
            frame = t * fps
            for col, alias in pairs:
                try:
                    v = float(row[col])
                except ValueError:
                    v = 0.0
                cmds.setKeyframe("%s.%s" % (node_name, alias),
                                 time=frame, value=v,
                                 inTangentType="linear",
                                 outTangentType="linear")
                total_keys += 1

    if total_keys == 0:
        cmds.warning("No ARKit-52 targets matched any blendShape node. "
                     "This bundle expects an ARKit-standard rig (ARKit "
                     "blendshape naming) -- see {readme_name}.")
    print("[apply] done. Set %d keys total." % total_keys)


main()
''')
    return target


def _write_blender_script(
    out_dir: Path,
    csv_filename: str,
    *,
    script_name: str = "apply_in_blender.py",
    readme_name: str = "README.txt",
) -> Path:
    """Write a Blender import script with CSV path baked in for end users.

    Parallel to _write_apply_script: auto-detects every mesh with shape
    keys and keys ARKit-52-named shape keys from the CSV. Zero-config.
    """
    target = out_dir / script_name
    target.write_text(f'''"""
{script_name} - Apply captured facial animation to your ARKit-52 mesh.

USAGE:

    1. Open your .blend scene containing the ARKit-rigged head.
    2. Run this script in Blender's Scripting workspace.

If you're not sure how to do step 2, see {readme_name} in this bundle
-- it walks through running this script with no scripting experience
assumed.

The script finds every mesh that has shape keys, sets the scene fps,
and keys the captured weights onto shape keys whose names match the
ARKit-52 columns. Shape keys that don't match are left untouched. If
nothing matches anywhere, the script reports it loudly -- this bundle
expects an ARKit-standard rig (ARKit-named shape keys).
"""

import csv
import os
import bpy


# ---- USER CONFIG ------------------------------------------------------

# The CSV ships next to this script and is found automatically. Only
# set CSV_PATH if you want to point at a CSV elsewhere; otherwise
# leave it "" and the script locates {csv_filename!r} on its own.
CSV_NAME = {csv_filename!r}
CSV_PATH = ""

# Leave this EMPTY to auto-detect every mesh in the scene that has
# shape keys (the normal case -- handles split ARKit rigs with
# head/eyes/teeth meshes automatically). Only fill it in to restrict
# application to specific objects in an unusual scene, e.g.:
#     MESH_OBJECTS = ["Face", "Eyes"]
MESH_OBJECTS = []  # type: list

# Frame rate the data was delivered at (24 is the source rate).
DATA_FPS = 24

# Erase any existing animation on matched shape keys before re-keying.
CLEAR_EXISTING = True

# ---- END USER CONFIG --------------------------------------------------


NON_TARGET_COLS = ("frame", "time_seconds", "detected",
                   "head_yaw", "head_pitch", "head_roll",
                   "head_tx", "head_ty", "head_tz")


# The 52 standard ARKit blendshape names. Used to recover a clean ARKit
# identity from shape keys that are namespaced or were mangled by a USD
# round-trip (e.g. _Mesh_ncl1_1_jawOpen_jawOpenShape). The CSV columns use
# clean ARKit names, so without this such a rig matches zero shape keys and
# you'd see the "no ARKit-52 shape keys matched" warning.
_ARKIT_52 = (
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft",
    "eyeLookUpLeft", "eyeSquintLeft", "eyeWideLeft",
    "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight",
    "eyeLookUpRight", "eyeSquintRight", "eyeWideRight",
    "jawForward", "jawLeft", "jawRight", "jawOpen",
    "mouthClose", "mouthFunnel", "mouthPucker", "mouthRight", "mouthLeft",
    "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
    "mouthDimpleLeft", "mouthDimpleRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthPressLeft", "mouthPressRight",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "noseSneerLeft", "noseSneerRight", "tongueOut",
)
_ARKIT_BY_LOWER = {{n.lower(): n for n in _ARKIT_52}}


def _canonical_arkit_name(rig_name):
    # Recover the ARKit name a (possibly namespaced/mangled) name encodes.
    # Drop a trailing "Shape", then take the longest ARKit name the result
    # ends with, on a name boundary so an embedded substring can't false-match
    # (mouthRight vs mouthStretchRight).
    low = rig_name.lower()
    if low.endswith("shape"):
        low = low[:-len("shape")]
    best = None
    for arkit_lower, arkit in _ARKIT_BY_LOWER.items():
        if not low.endswith(arkit_lower):
            continue
        boundary = len(low) - len(arkit_lower)
        if boundary != 0 and low[boundary - 1].isalnum():
            continue
        if best is None or len(arkit_lower) > len(best.lower()):
            best = arkit
    return best


def _match_columns(target_cols, rig_names):
    # Pair each clean-ARKit CSV column with the shape key that encodes it.
    # Case-insensitive direct match first; canonical recovery as a fallback.
    # Returns [(csv_col, shape_key_name)]; shape_key_name is what we key onto.
    by_key = {{}}
    for rn in rig_names:
        by_key.setdefault(rn.lower(), rn)
        canon = _canonical_arkit_name(rn)
        if canon is not None:
            by_key.setdefault(canon.lower(), rn)
    pairs = []
    for c in target_cols:
        rn = by_key.get(c.lower())
        if rn is not None:
            pairs.append((c, rn))
    return pairs


def _mesh_objects():
    if MESH_OBJECTS:
        objs = []
        for nm in MESH_OBJECTS:
            o = bpy.data.objects.get(nm)
            if o is None:
                print("[apply] WARNING: object not found, skipping: %s" % nm)
            else:
                objs.append(o)
        return objs
    return [o for o in bpy.data.objects
            if o.type == "MESH" and o.data.shape_keys]


def _resolve_csv():
    # 1. Explicit override wins.
    if CSV_PATH and os.path.isfile(CSV_PATH):
        return CSV_PATH
    candidates = []
    # 2. Next to this script, when run as a file (blender --python).
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        # __file__ is undefined when run from the Text Editor.
        pass
    # 3. Folder of this script's text datablock, when opened via
    #    Text > Open in the Scripting workspace (the normal flow).
    for txt in bpy.data.texts:
        fp = getattr(txt, "filepath", "")
        if fp and os.path.basename(fp) == {script_name!r}:
            candidates.append(os.path.dirname(bpy.path.abspath(fp)))
    # 4. The .blend file's folder, then the working directory.
    if bpy.data.filepath:
        candidates.append(os.path.dirname(bpy.data.filepath))
    candidates.append(os.getcwd())
    for d in candidates:
        cand = os.path.join(d, CSV_NAME)
        if os.path.isfile(cand):
            return cand
    return None


def main():
    csv_path = _resolve_csv()
    if not csv_path:
        raise RuntimeError(
            "Could not locate %s. Open it via Text > Open in the "
            "Scripting workspace, or set CSV_PATH at the top of this "
            "script to its full path." % CSV_NAME)

    scene = bpy.context.scene
    scene.render.fps = DATA_FPS
    scene.render.fps_base = 1.0
    fps = DATA_FPS

    print("[apply] using CSV: %s" % csv_path)
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames or []

    if not rows:
        raise RuntimeError("CSV is empty.")

    target_cols = [c for c in header if c not in NON_TARGET_COLS]
    print("[apply] %d data columns in CSV" % len(target_cols))
    print("[apply] %d frames" % len(rows))

    objs = _mesh_objects()
    if not objs:
        raise RuntimeError(
            "No meshes with shape keys found in the scene. Open your "
            "ARKit-rigged scene before running this script.")
    print("[apply] applying to %d mesh object(s): %s"
          % (len(objs), ", ".join(o.name for o in objs)))

    last_t = float(rows[-1]["time_seconds"])
    last_frame = int(round(last_t * fps))
    scene.frame_start = 0
    scene.frame_end = last_frame

    total_keys = 0
    for obj in objs:
        key_blocks = obj.data.shape_keys.key_blocks
        pairs = _match_columns(target_cols, [kb.name for kb in key_blocks])
        print("[apply] %s: matched %d/%d shape keys"
              % (obj.name, len(pairs), len(key_blocks)))

        if CLEAR_EXISTING:
            adata = obj.data.shape_keys.animation_data
            if adata and adata.action:
                names = [sk for _, sk in pairs]
                for fc in list(adata.action.fcurves):
                    dp = fc.data_path
                    if dp.endswith(".value") and any(
                            ('["%s"]' % m) in dp for m in names):
                        adata.action.fcurves.remove(fc)

        for row in rows:
            try:
                t = float(row["time_seconds"])
            except (ValueError, KeyError):
                continue
            frame = int(round(t * fps))
            for col, sk in pairs:
                try:
                    v = float(row[col])
                except ValueError:
                    v = 0.0
                kb = key_blocks[sk]
                kb.value = v
                kb.keyframe_insert(data_path="value", frame=frame)
                total_keys += 1

    # Linear interpolation, matching the Maya script's linear tangents.
    for obj in objs:
        adata = obj.data.shape_keys.animation_data
        if adata and adata.action:
            for fc in adata.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"

    if total_keys == 0:
        print("[apply] WARNING: No ARKit-52 shape keys matched any mesh. "
              "This bundle expects an ARKit-standard rig (ARKit-named "
              "shape keys) -- see {readme_name}.")
    print("[apply] done. Set %d keys total." % total_keys)


main()
''')
    return target


def _write_readme(out_dir: Path, csv_filename: str,
                  source_filename: str, n_frames: int,
                  *,
                  readme_name: str = "README.txt",
                  preview_name: str = "preview.mp4",
                  maya_name: str = "apply_in_maya.py",
                  blender_name: str = "apply_in_blender.py",
                  usd_head_name: str | None = None,
                  usd_animated_name: str | None = None) -> Path:
    # USD outputs are optional — only included if the one-off Maya export
    # has been done. README sections are toggled off when absent so the
    # bundle docs stay accurate for whatever shipped.
    has_usd = usd_head_name is not None and usd_animated_name is not None
    usd_contents = (f"""
  {usd_head_name}
      Static neutral ARKit head mesh (USD binary). Contains the 51
      blendshape targets the animation drives.

  {usd_animated_name}
      The same head with this clip's animation already baked on. Open
      it in any USD-capable DCC (Maya 2022+, Blender 3.5+, Houdini,
      Omniverse, Unreal Sequencer) and press play. Must stay in the
      same folder as {usd_head_name} -- it sublayers it by relative
      path."""
                    if has_usd else "")
    usd_quickplay = (f"""

Quick play (USD, no scripting):
  1. Unzip this bundle.
  2. Drag {usd_animated_name} into your DCC -- or File > Open it.
       Maya 2022+      : USD stage opens in the viewport.
       Blender 3.5+    : File > Import > Universal Scene Description.
       Houdini         : File > Open, or a SOP-level usdimport.
       Omniverse       : double-click in the Content browser.
       Unreal Engine 5 : Content Browser > Import, or drop into a
                         level via the USD Stage panel.
  3. Press play on the timeline. The animation is baked in.

  Note: {usd_animated_name} *references* {usd_head_name} by relative
  path, so keep both files in the same folder. Moving just the .usda
  will load an empty stage."""
                     if has_usd else "")
    target = out_dir / readme_name
    target.write_text(f"""Facial Capture Bundle
=====================

Source:        {source_filename}
Frames (24fps): {n_frames}

Files are named after your clip so bundles don't get mixed up.

Contents:
  {preview_name}
      Preview of tracking quality on your source footage. Mesh overlay
      shows what was tracked; the corner bar graph shows the captured
      blendshape values.

  {csv_filename}
      The captured animation data. 24fps, ARKit-52 column names
      matching the standard blendshape targets.

  {maya_name}
      Maya script to apply the CSV to your rig.

  {blender_name}
      Blender script to apply the CSV to your rig.{usd_contents}

Use whichever matches your DCC. Both scripts are zero-config: they
auto-detect your rig and the CSV path -- no names to look up, no paths
to edit.{(" Or, if your DCC supports USD, just open " + (usd_animated_name or "")
+ " for an instant playback of the supplied ARKit head.") if has_usd else ""}{usd_quickplay}

How to use in Maya (no scripting experience needed):
  1. Open your Maya scene containing the ARKit-rigged head mesh.

  2. Open the Script Editor. In Maya's top menu bar:
         Windows  >  General Editors  >  Script Editor
     (You can also click the {{;}} icon at the bottom-right of the
     Maya window.)

  3. In the Script Editor window, click the tab labelled "Python".
     This matters -- there is also a "MEL" tab, and this script is
     Python. If you only see one input area, use the Script Editor
     menu: Command > (make sure Python is selected).

  4. Load this script into that Python tab. In the Script Editor's
     own menu:
         File  >  Open Script...
     then browse to this bundle folder and choose {maya_name}.
     The script's text appears in the lower (input) panel.

  5. Run it. Either:
         - click "Execute All" -- the double blue arrow button on
           the Script Editor toolbar, or
         - press Ctrl + Enter   (Cmd + Enter on macOS).

  6. If a file dialog appears asking for {csv_filename}, pick it
     from this unzipped bundle folder. (It only appears if the script
     couldn't find the CSV automatically -- e.g. you pasted the code
     in rather than opening the file. Picking it once is all it needs.)

  7. Watch the upper (output) panel. The script prints progress and
     finishes with a line like:
         [apply] done. Set 12345 keys total.
     Press play on the timeline to see the animation.

No node names to look up, no file paths to edit -- the script finds
everything on its own.

How to use in Blender (no scripting experience needed):
  1. Open your .blend scene containing the ARKit-rigged head mesh.

  2. At the top of the Blender window, click the "Scripting" tab/
     workspace. A text editor and a Python console appear.

  3. In the text editor's header, click "Open" (folder icon), browse
     to this bundle folder, and choose {blender_name}.

  4. Run it. Either:
         - click the "Run Script" button (the ▶ play icon in the
           text editor header), or
         - hover the mouse over the text editor and press Alt + P.

  5. Open the System Console / Toggle System Console (Window menu, on
     Windows) or the terminal Blender was launched from to see the
     progress lines, ending with:
         [apply] done. Set 12345 keys total.
     Press play on the timeline to see the animation.

Same as the Maya script: nothing to look up or edit.

Notes:
  - Both scripts auto-detect the rig. Maya: every blendShape node in
    the scene. Blender: every mesh that has shape keys. Split ARKit
    rigs (head + eyes + teeth) are handled with no extra steps.
  - Scene fps is set to 24 by the script (matches the source).
  - The CSV uses standard ARKit-52 names. Maya targets / Blender shape
    keys named differently on your rig are skipped; if nothing matches
    anywhere, the script warns you (this bundle expects an
    ARKit-standard rig).
  - The script locates {csv_filename} on its own. If it can't, Maya
    pops a one-time file picker; Blender prints a clear message. As a
    last resort, set CSV_PATH at the top of the script to the CSV's
    full path.
  - Unusual scene? Set BLENDSHAPE_NODES (Maya) or MESH_OBJECTS
    (Blender) to restrict application to specific nodes/objects.
""")
    return target


def run_pipeline(
    source_video: Path,
    out_dir: Path,
    *,
    interpolate: bool = True,
    keep_intermediate: bool = False,
) -> dict:
    """Run the full pipeline. Returns paths of the four deliverables."""

    if not source_video.exists():
        raise FileNotFoundError(source_video)

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="facecap_work_"))
    print(f"[pipeline] work dir: {work_dir}")
    print(f"[pipeline] output dir: {out_dir}")

    try:
        source_fps = _detect_source_fps(source_video)
        print(f"[pipeline] source fps: {source_fps:.2f}")

        # Step 1: crop
        cropped = work_dir / "cropped.mp4"
        _run(
            [sys.executable, str(HERE / "crop.py"),
             str(source_video), "-o", str(cropped)],
            "crop"
        )

        # Step 2: interpolate (optional but on by default for quality)
        if interpolate:
            interpolated = work_dir / "interp_60.mp4"
            _run(
                ["ffmpeg", "-loglevel", "error", "-y",
                 "-i", str(cropped),
                 "-vf", "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:"
                        "me_mode=bidir:vsbmc=1",
                 "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                 str(interpolated)],
                "interpolate to 60fps"
            )
            capture_input = interpolated
            capture_fps = 60.0
        else:
            capture_input = cropped
            capture_fps = source_fps

        # Step 3: capture
        blendshapes_raw = work_dir / "blendshapes_raw.csv"
        _run(
            [sys.executable, str(HERE / "capture.py"),
             str(capture_input), "-o", str(blendshapes_raw)],
            "capture blendshapes"
        )

        # Step 4: smooth (cutoff tuned to capture fps)
        blendshapes_smoothed = work_dir / "blendshapes_smoothed.csv"
        min_cutoff = "2.5" if capture_fps >= 50 else "1.5"
        _run(
            [sys.executable, str(HERE / "smooth.py"), str(blendshapes_raw),
             "-o", str(blendshapes_smoothed), "--min-cutoff", min_cutoff],
            f"smooth (min_cutoff={min_cutoff})"
        )

        # Deliverables are named after the source clip so the user can
        # tell bundles apart (e.g. shot42_preview.mp4).
        stem = _safe_stem(source_video.stem)
        maya_name = f"{stem}_apply_in_maya.py"
        blender_name = f"{stem}_apply_in_blender.py"
        readme_name = f"{stem}_README.txt"

        # Step 5: resample to source fps for delivery
        out_csv_name = f"{stem}_blendshapes.csv"
        blendshapes_out = out_dir / out_csv_name
        _run(
            [sys.executable, str(HERE / "resample.py"),
             str(blendshapes_smoothed),
             "-o", str(blendshapes_out),
             "--target-fps", str(source_fps)],
            f"resample to {source_fps:.2f} fps"
        )

        # Step 6: preview overlay against the original source
        preview_out = out_dir / f"{stem}_preview.mp4"
        _run(
            [sys.executable, str(HERE / "preview_overlay.py"),
             str(source_video), str(blendshapes_out),
             "-o", str(preview_out)],
            "render preview overlay"
        )

        # Step 7: write apply scripts and README
        apply_script = _write_apply_script(
            out_dir, out_csv_name,
            script_name=maya_name, readme_name=readme_name,
        )
        blender_script = _write_blender_script(
            out_dir, out_csv_name,
            script_name=blender_name, readme_name=readme_name,
        )

        # Count output frames for README
        import csv as csvmod
        with blendshapes_out.open() as f:
            n_frames = sum(1 for _ in csvmod.reader(f)) - 1

        # Step 8: USD bundle assets (skipped silently if the one-off Maya
        # export hasn't been run yet — see scripts/export_arkit_head_to_usd.py).
        # Both files are best-effort: a malformed asset must not lose the
        # user the CSV + apply scripts they actually came for.
        head_src = HERE / "assets" / "arkit_head.usdc"
        head_usd_dest: Path | None = None
        animated_usd: Path | None = None
        if head_src.is_file():
            try:
                # Support both package import (`from pipeline.orchestrator
                # import run_pipeline`, the web-service path) and direct
                # script invocation (`python pipeline/orchestrator.py`,
                # where there's no parent package).
                try:
                    from .usd_export import bake_animated_usd
                except ImportError:
                    from usd_export import bake_animated_usd  # type: ignore
                head_usd_dest = out_dir / f"{stem}_arkit_head.usdc"
                shutil.copy2(head_src, head_usd_dest)
                animated_usd = out_dir / f"{stem}_animated.usda"
                bake = bake_animated_usd(
                    blendshapes_out, head_usd_dest, animated_usd, fps=source_fps,
                )
                print(
                    f"[pipeline] USD: {animated_usd.name} "
                    f"({bake.frame_count} frames, "
                    f"{len(bake.matched_columns)} matched / "
                    f"{len(bake.unmatched_columns)} unmatched)"
                )
            except Exception as exc:
                # Don't fail the job over a USD problem; CSV + scripts are
                # the primary deliverables. Surface in logs and move on.
                print(f"[pipeline] WARNING: USD bake skipped: {exc}",
                      file=sys.stderr)
                if animated_usd and animated_usd.exists():
                    animated_usd.unlink()
                if head_usd_dest and head_usd_dest.exists():
                    head_usd_dest.unlink()
                head_usd_dest = None
                animated_usd = None
        else:
            print(
                f"[pipeline] note: {head_src.name} not found in "
                f"pipeline/assets/ — skipping USD bake. Run "
                "scripts/export_arkit_head_to_usd.py to enable."
            )

        # README is written last so it can list whichever USD files actually
        # made it into the bundle.
        readme = _write_readme(
            out_dir, out_csv_name, source_video.name, n_frames,
            readme_name=readme_name, preview_name=preview_out.name,
            maya_name=maya_name, blender_name=blender_name,
            usd_head_name=head_usd_dest.name if head_usd_dest else None,
            usd_animated_name=animated_usd.name if animated_usd else None,
        )

        print("\n[pipeline] DELIVERABLES:")
        emitted = [preview_out, blendshapes_out, apply_script,
                   blender_script, readme]
        if head_usd_dest is not None:
            emitted.append(head_usd_dest)
        if animated_usd is not None:
            emitted.append(animated_usd)
        for p in emitted:
            print(f"  {p}")

        result: dict = {
            "preview": preview_out,
            "csv": blendshapes_out,
            "apply_script": apply_script,
            "blender_script": blender_script,
            "readme": readme,
        }
        if head_usd_dest is not None:
            result["head_usd"] = head_usd_dest
        if animated_usd is not None:
            result["animated_usd"] = animated_usd
        return result
    finally:
        if not keep_intermediate:
            shutil.rmtree(work_dir, ignore_errors=True)
            print(f"\n[pipeline] cleaned up work dir")
        else:
            print(f"\n[pipeline] intermediate kept at: {work_dir}")


def make_zip(
    deliverables: dict, zip_path: Path, *, root_dir: str | None = None
) -> Path:
    """Zip the deliverables. If root_dir is given, the files are placed
    under a single top-level folder of that name, so unzipping yields a
    tidy `<clip>/` directory rather than loose files."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, path in deliverables.items():
            arcname = f"{root_dir}/{path.name}" if root_dir else path.name
            zf.write(path, arcname=arcname)
    print(f"[pipeline] zipped -> {zip_path}")
    return zip_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--output", "-o", type=Path, required=True,
                    help="Output directory")
    ap.add_argument("--no-interpolate", dest="interpolate",
                    action="store_false",
                    help="Skip the 60fps interpolation step (faster, lower quality)")
    ap.add_argument("--zip", action="store_true",
                    help="Also produce a .zip of the bundle")
    ap.add_argument("--keep-intermediate", action="store_true",
                    help="Keep working files for debugging")
    args = ap.parse_args()

    deliverables = run_pipeline(
        args.video, args.output,
        interpolate=args.interpolate,
        keep_intermediate=args.keep_intermediate,
    )

    if args.zip:
        stem = _safe_stem(args.video.stem)
        zip_path = args.output.parent / f"{stem}.zip"
        make_zip(deliverables, zip_path, root_dir=stem)


if __name__ == "__main__":
    main()

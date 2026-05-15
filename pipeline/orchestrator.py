"""
pipeline.py - End-to-end pipeline orchestrator.

Takes a source video and produces the user delivery bundle:
    preview.mp4         (mesh overlay on 24fps source)
    blendshapes.csv     (24fps ARKit-52, ready for Maya import)
    apply_in_maya.py    (the Maya import script, with CSV path baked in)
    README.txt          (usage instructions)

All four files are written into a single output directory (or zipped if
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
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).parent


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


def _write_apply_script(out_dir: Path, csv_filename: str) -> Path:
    """Write a Maya import script with CSV path baked in for end users."""
    target = out_dir / "apply_in_maya.py"
    target.write_text(f'''"""
apply_in_maya.py - Apply captured facial animation to your ARKit-52 mesh.

USAGE:

    1. Open your Maya scene containing the ARKit-rigged head.
    2. Run this script in Maya's Script Editor (Python tab).

If you're not sure how to do step 2, see README.txt in this bundle --
it walks through opening the Script Editor and running this script
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
        matched = [c for c in target_cols if c in targets]
        print("[apply] %s: matched %d/%d targets"
              % (node_name, len(matched), len(targets)))

        if CLEAR_EXISTING:
            for name in matched:
                try:
                    cmds.cutKey("%s.%s" % (node_name, name), clear=True)
                except Exception:
                    pass

        for row in rows:
            try:
                t = float(row["time_seconds"])
            except (ValueError, KeyError):
                continue
            frame = t * fps
            for name in matched:
                try:
                    v = float(row[name])
                except ValueError:
                    v = 0.0
                cmds.setKeyframe("%s.%s" % (node_name, name),
                                 time=frame, value=v,
                                 inTangentType="linear",
                                 outTangentType="linear")
                total_keys += 1

    if total_keys == 0:
        cmds.warning("No ARKit-52 targets matched any blendShape node. "
                     "This bundle expects an ARKit-standard rig (ARKit "
                     "blendshape naming) -- see README.txt.")
    print("[apply] done. Set %d keys total." % total_keys)


main()
''')
    return target


def _write_blender_script(out_dir: Path, csv_filename: str) -> Path:
    """Write a Blender import script with CSV path baked in for end users.

    Parallel to _write_apply_script: auto-detects every mesh with shape
    keys and keys ARKit-52-named shape keys from the CSV. Zero-config.
    """
    target = out_dir / "apply_in_blender.py"
    target.write_text(f'''"""
apply_in_blender.py - Apply captured facial animation to your ARKit-52 mesh.

USAGE:

    1. Open your .blend scene containing the ARKit-rigged head.
    2. Run this script in Blender's Scripting workspace.

If you're not sure how to do step 2, see README.txt in this bundle --
it walks through running this script with no scripting experience
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
        if fp and os.path.basename(fp) == "apply_in_blender.py":
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
        matched = [c for c in target_cols if c in key_blocks]
        print("[apply] %s: matched %d/%d shape keys"
              % (obj.name, len(matched), len(key_blocks)))

        if CLEAR_EXISTING:
            adata = obj.data.shape_keys.animation_data
            if adata and adata.action:
                for fc in list(adata.action.fcurves):
                    dp = fc.data_path
                    if dp.endswith(".value") and any(
                            ('["%s"]' % m) in dp for m in matched):
                        adata.action.fcurves.remove(fc)

        for row in rows:
            try:
                t = float(row["time_seconds"])
            except (ValueError, KeyError):
                continue
            frame = int(round(t * fps))
            for name in matched:
                try:
                    v = float(row[name])
                except ValueError:
                    v = 0.0
                kb = key_blocks[name]
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
              "shape keys) -- see README.txt.")
    print("[apply] done. Set %d keys total." % total_keys)


main()
''')
    return target


def _write_readme(out_dir: Path, csv_filename: str,
                  source_filename: str, n_frames: int) -> Path:
    target = out_dir / "README.txt"
    target.write_text(f"""Facial Capture Bundle
=====================

Source:        {source_filename}
Frames (24fps): {n_frames}

Contents:
  preview.mp4         - Preview of tracking quality on your source footage.
                        Mesh overlay shows what was tracked; the bar graph
                        in the corner shows the captured blendshape values.

  {csv_filename}   - The captured animation data. 24fps, ARKit-52 column
                        names matching the standard blendshape targets.

  apply_in_maya.py    - Maya script to apply the CSV to your rig.

  apply_in_blender.py - Blender script to apply the CSV to your rig.

Use whichever matches your DCC. Both are zero-config: they auto-detect
your rig and the CSV path -- no names to look up, no paths to edit.

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
     then browse to this bundle folder and choose apply_in_maya.py.
     The script's text appears in the lower (input) panel.

  5. Run it. Either:
         - click "Execute All" -- the double blue arrow button on
           the Script Editor toolbar, or
         - press Ctrl + Enter   (Cmd + Enter on macOS).

  6. If a file dialog appears asking for blendshapes.csv, pick it
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
     to this bundle folder, and choose apply_in_blender.py.

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
  - The script locates blendshapes.csv on its own. If it can't, Maya
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

        # Step 5: resample to source fps for delivery
        out_csv_name = "blendshapes.csv"
        blendshapes_out = out_dir / out_csv_name
        _run(
            [sys.executable, str(HERE / "resample.py"),
             str(blendshapes_smoothed),
             "-o", str(blendshapes_out),
             "--target-fps", str(source_fps)],
            f"resample to {source_fps:.2f} fps"
        )

        # Step 6: preview overlay against the original source
        preview_out = out_dir / "preview.mp4"
        _run(
            [sys.executable, str(HERE / "preview_overlay.py"),
             str(source_video), str(blendshapes_out),
             "-o", str(preview_out)],
            "render preview overlay"
        )

        # Step 7: write apply scripts and README
        apply_script = _write_apply_script(out_dir, out_csv_name)
        blender_script = _write_blender_script(out_dir, out_csv_name)

        # Count output frames for README
        import csv as csvmod
        with blendshapes_out.open() as f:
            n_frames = sum(1 for _ in csvmod.reader(f)) - 1

        readme = _write_readme(
            out_dir, out_csv_name, source_video.name, n_frames
        )

        print("\n[pipeline] DELIVERABLES:")
        for p in [preview_out, blendshapes_out, apply_script,
                  blender_script, readme]:
            print(f"  {p}")

        return {
            "preview": preview_out,
            "csv": blendshapes_out,
            "apply_script": apply_script,
            "blender_script": blender_script,
            "readme": readme,
        }
    finally:
        if not keep_intermediate:
            shutil.rmtree(work_dir, ignore_errors=True)
            print(f"\n[pipeline] cleaned up work dir")
        else:
            print(f"\n[pipeline] intermediate kept at: {work_dir}")


def make_zip(deliverables: dict, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, path in deliverables.items():
            zf.write(path, arcname=path.name)
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
        zip_path = args.output.parent / f"{args.output.name}.zip"
        make_zip(deliverables, zip_path)


if __name__ == "__main__":
    main()

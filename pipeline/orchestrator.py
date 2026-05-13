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
    if "/" in out:
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 24.0
    return float(out)


def _write_apply_script(out_dir: Path, csv_filename: str) -> Path:
    """Write a Maya import script with CSV path baked in for end users."""
    target = out_dir / "apply_in_maya.py"
    target.write_text(f'''"""
apply_in_maya.py - Apply captured facial animation to your ARKit-52 mesh.

USAGE (run from inside Maya's Script Editor, Python tab):

    1. Open your Maya scene containing the ARKit-rigged head.

    2. Set Maya's scene fps to match the data (24fps):
           import maya.cmds as cmds
           cmds.currentUnit(time='film')

    3. Update BLENDSHAPE_NODES below if your blendShape nodes are not named
       as defaults. Run this script:
           import maya.cmds as cmds
           print(cmds.ls(type='blendShape'))
       to discover the names.

    4. Update CSV_PATH below to point to where you unzipped this bundle.

    5. Run this script.

The script keys blendshape weights from the CSV onto matching ARKit-named
targets on each blendShape node. Columns that don't match a target on a
given node are skipped silently.
"""

import csv
import os
import maya.cmds as cmds


# ---- USER CONFIG ------------------------------------------------------

# Path to the CSV file (in the same folder as this script by default).
# Change if you moved the CSV elsewhere.
CSV_PATH = os.path.join(os.path.dirname(__file__), {csv_filename!r})

# One or more blendShape node names. Run cmds.ls(type='blendShape') in
# Maya to discover them. For a typical ARKit FBX rig with split meshes
# (head/eyes/teeth) you'll have several. The script handles unmatched
# columns per-node gracefully.
BLENDSHAPE_NODES = [
    "blendShape1",
    # Add more here if your rig has additional blendShape nodes,
    # e.g. for eye and teeth meshes:
    # "Mesh_ncl1_1",
    # "MeshFBXASC046001_ncl1_1",
    # "MeshFBXASC046002_ncl1_1",
    # "MeshFBXASC046003_ncl1_1",
]

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


def main():
    if not os.path.isfile(CSV_PATH):
        cmds.error("CSV not found: %s" % CSV_PATH)
        return

    # Set scene fps
    cmds.currentUnit(time=SCENE_FPS)
    fps_map = {{"game": 15, "film": 24, "pal": 25, "ntsc": 30,
                "show": 48, "palf": 50, "ntscf": 60}}
    fps = fps_map.get(SCENE_FPS, 24)

    with open(CSV_PATH, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames

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

    total_keys = 0
    for node_name in BLENDSHAPE_NODES:
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

How to use in Maya:
  1. Open your scene with the ARKit-rigged head mesh.
  2. In Maya's Script Editor, find your blendShape node name(s):
         import maya.cmds as cmds
         print(cmds.ls(type="blendShape"))
  3. Edit BLENDSHAPE_NODES in apply_in_maya.py to match those name(s).
  4. Drop the script into Maya's Script Editor and run.

Notes:
  - Scene fps will be set to 24 by the script (matches the source).
  - If your rig has multiple blendShape nodes (e.g. head + eyes + teeth),
    add each one to BLENDSHAPE_NODES. The script handles per-node target
    matching automatically.
  - The CSV uses standard ARKit-52 names. Targets named differently
    on your rig will be skipped with a warning.
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

        # Step 7: write apply_in_maya.py and README
        apply_script = _write_apply_script(out_dir, out_csv_name)

        # Count output frames for README
        import csv as csvmod
        with blendshapes_out.open() as f:
            n_frames = sum(1 for _ in csvmod.reader(f)) - 1

        readme = _write_readme(
            out_dir, out_csv_name, source_video.name, n_frames
        )

        print("\n[pipeline] DELIVERABLES:")
        for p in [preview_out, blendshapes_out, apply_script, readme]:
            print(f"  {p}")

        return {
            "preview": preview_out,
            "csv": blendshapes_out,
            "apply_script": apply_script,
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

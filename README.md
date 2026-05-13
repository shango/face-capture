# Facial Capture Pipeline

Markerless monocular facial capture from video → ARKit-52 blendshape data → Maya rig.

## Pipeline overview

```
source.mp4 (24fps)
  └─ crop.py            face-aware square crop, upscaled to 1080p
       └─ ffmpeg interp 24 → 60fps temporal interpolation (minterpolate)
            └─ capture.py     MediaPipe → 52 ARKit blendshapes per frame
                 └─ smooth.py     One Euro filter
                      └─ resample.py   60 → 24fps for delivery
                           └─ preview_overlay.py  mesh overlay on source
                                └─ DELIVERABLES: preview.mp4 + CSV + apply_in_maya.py
```

All five steps are orchestrated by `pipeline.py` — one command runs everything.

## Setup

System dependencies (one time, requires sudo):

```bash
sudo apt update
sudo apt install ffmpeg
```

Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Download the MediaPipe Face Landmarker model (one time, 3.7 MB):

```bash
wget https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
```

Place `face_landmarker.task` in the project root.

## Usage

### Run the full pipeline (recommended)

```bash
python pipeline.py source.mp4 -o ./jobs/myjob --zip
```

Produces `./jobs/myjob/` containing:
- `preview.mp4` — mesh overlay on the source video (24fps)
- `blendshapes.csv` — 24fps ARKit-52 named columns, ready for Maya
- `apply_in_maya.py` — script to drop into Maya's Script Editor
- `README.txt` — user-facing instructions

With `--zip`, also produces `./jobs/myjob.zip` of all four files.

### Flags

- `--no-interpolate`: skip the 60fps interpolation step. Fast (~30s) but lower quality. Useful for iteration.
- `--keep-intermediate`: keep working files for debugging.
- `--zip`: also produce a .zip of the bundle.

### Apply in Maya

Inside Maya's Script Editor (Python tab):

1. Find your blendShape node names:
   ```python
   import maya.cmds as cmds
   print(cmds.ls(type='blendShape'))
   ```
2. Open `apply_in_maya.py`, set `BLENDSHAPE_NODES = [...]` with those names.
3. If the script and CSV aren't in the same folder, set `CSV_PATH` explicitly.
4. Run the script. Animation will be keyed to your rig.

For ARKit FBX rigs with separate head/eyes/teeth meshes, list all the relevant blendShape nodes — the script handles per-node target matching automatically.

## Individual scripts (used by pipeline.py, but callable standalone)

| Script | Purpose |
|---|---|
| `crop.py` | Face-aware square crop, upscale to 1080p |
| `capture.py` | MediaPipe inference → CSV of ARKit-52 weights + head pose |
| `smooth.py` | One Euro filter to reduce per-frame jitter |
| `resample.py` | Linear interpolation between frame rates |
| `preview_overlay.py` | Render mesh overlay on source using CSV data |
| `interp.py` | Standalone interpolation wrapper (RIFE or ffmpeg) |
| `pipeline.py` | Orchestrator running the full flow |

## Auxiliary scripts (not part of the main pipeline)

| Script | Purpose |
|---|---|
| `retarget.py` | Map ARKit-52 to a custom blendshape set via JSON mapping |
| `compare.py` | Plot any column across multiple CSVs (A/B comparisons) |
| `preview_rich.py` | Diagnostic mesh+bar overlay with re-solve (heavier than preview_overlay) |
| `maya_import.py` | Older callable Maya import module (apply_in_maya.py is the per-job version) |

## Notes on quality

- **Cropping** is the cheapest improvement; keep it in the pipeline.
- **Interpolation** improves slow/sustained motion but doesn't recover information lost between 24fps samples. `minterpolate` is CPU-only; for higher quality use RIFE via `interp.py --backend rife` (requires the `rife-ncnn-vulkan` binary).
- **Smoothing** filter cutoff is auto-tuned to capture fps inside `pipeline.py` — 2.5 Hz at 60fps capture, 1.5 Hz at 24fps capture.
- **Mesh slipping** is most noticeable in the forehead/eyebrow region. This is a known limitation of MediaPipe's generic prior; cropping + smoothing reduces but doesn't eliminate it.

## Next steps for Claude Code

- Validate the local pipeline end-to-end (`python pipeline.py test.mp4 -o ./jobs/test --no-interpolate` for a fast test, then with interpolation for the final).
- Add per-channel smoothing (heavier filter on brow channels specifically).
- Add per-target scaling/curves in retarget step (eye blinks usually need 1.5–2x boost).
- Consider FastAPI wrapper around `pipeline.run_pipeline()` for hosted deployment on Railway.

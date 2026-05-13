"""
crop.py - Face-aware video crop preprocessor.

Detects the face on a sample of frames, computes a stable square crop with
margin, and outputs a video reframed to put the face in the middle and fill
the frame. Significantly improves capture quality on wider shots by giving
MediaPipe more effective pixels per facial feature.

Run BEFORE capture.py:
    python crop.py source.mp4 -o source_cropped.mp4
    python capture.py source_cropped.mp4 -o blendshapes.csv

Detection strategy:
    - Samples N frames evenly spaced across the clip
    - Runs MediaPipe FaceDetector on each (lighter than the full landmarker)
    - Uses the median bounding box of successful detections to define crop
    - Adds configurable margin (default 30%) around the detected face
    - Produces a square crop, upscaled to --target-size (default 1080)

Why detect on multiple frames:
    A single-frame detection can be off if the actor blinks, turns slightly,
    or is partially occluded on that frame. The median across samples is
    robust to those outliers.

Requirements:
    pip install mediapipe>=0.10.14 opencv-python numpy
    ffmpeg on PATH
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def sample_face_bboxes(video_path: Path, n_samples: int) -> list[tuple[int, int, int, int]]:
    """Sample N frames, detect the face on each, return list of (x, y, w, h) bboxes."""
    # The lighter FaceDetector solution is gone in newer MediaPipe, so we
    # use the full landmarker on N frames and compute a bbox from its landmarks.
    # This is overkill but reuses what's already installed.
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    model_path = Path(__file__).parent / "face_landmarker.task"
    if not model_path.exists():
        sys.exit(f"face_landmarker.task not found at {model_path}. "
                 "Download from the MediaPipe model URL.")

    base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.IMAGE,
        num_faces=1,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
    )
    landmarker = mp_vision.FaceLandmarker.create_from_options(options)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        sys.exit(f"Could not open: {video_path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Evenly-spaced sample frame indices (skip first/last 5% to avoid pre/post-roll)
    margin = max(1, total // 20)
    indices = np.linspace(margin, total - margin, n_samples, dtype=int)

    bboxes = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = landmarker.detect(mp_img)
        if not result.face_landmarks:
            continue
        lms = result.face_landmarks[0]
        xs = np.array([lm.x for lm in lms]) * w
        ys = np.array([lm.y for lm in lms]) * h
        x_min, x_max = float(xs.min()), float(xs.max())
        y_min, y_max = float(ys.min()), float(ys.max())
        bx = int(x_min)
        by = int(y_min)
        bw = int(x_max - x_min)
        bh = int(y_max - y_min)
        bboxes.append((bx, by, bw, bh))

    cap.release()
    landmarker.close()
    return bboxes


def compute_crop_box(
    bboxes: list[tuple[int, int, int, int]],
    src_w: int, src_h: int,
    margin_pct: float,
) -> tuple[int, int, int]:
    """Return (crop_x, crop_y, crop_size) for a square crop centered on the
    median face position, expanded by margin_pct.

    Clamped to source frame bounds; if expansion would overflow, the crop
    is centered on the face but shrunk to fit.
    """
    if not bboxes:
        sys.exit("No faces detected in any sampled frame. Check the source.")

    # Median of each component for robustness
    xs = [b[0] for b in bboxes]
    ys = [b[1] for b in bboxes]
    ws = [b[2] for b in bboxes]
    hs = [b[3] for b in bboxes]

    bx = int(statistics.median(xs))
    by = int(statistics.median(ys))
    bw = int(statistics.median(ws))
    bh = int(statistics.median(hs))

    # Center of face
    cx = bx + bw // 2
    cy = by + bh // 2

    # Square crop size = max(width, height) * (1 + margin)
    base = max(bw, bh)
    size = int(base * (1.0 + margin_pct))

    # Clamp size to fit in frame
    size = min(size, src_w, src_h)

    # Compute top-left, clamped to frame
    crop_x = max(0, min(src_w - size, cx - size // 2))
    crop_y = max(0, min(src_h - size, cy - size // 2))

    return crop_x, crop_y, size


def run_ffmpeg_crop(
    src: Path, dst: Path,
    crop_x: int, crop_y: int, crop_size: int,
    target_size: int,
    crf: int,
) -> None:
    vf = f"crop={crop_size}:{crop_size}:{crop_x}:{crop_y},scale={target_size}:{target_size}"
    cmd = [
        "ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
        "-preset", "medium",
        "-c:a", "copy",
        str(dst),
    ]
    # If source has no audio, -c:a copy will fail; try fallback
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Retry without audio
        cmd_no_audio = [c for c in cmd if c not in ("-c:a", "copy")]
        subprocess.run(cmd_no_audio, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--output", "-o", type=Path, required=True)
    ap.add_argument("--samples", type=int, default=12,
                    help="Number of frames to sample for face detection (default 12)")
    ap.add_argument("--margin", type=float, default=0.30,
                    help="Margin around the face as fraction of face size "
                         "(default 0.30 = 30%%)")
    ap.add_argument("--target-size", type=int, default=1080,
                    help="Output square dimension in pixels (default 1080)")
    ap.add_argument("--crf", type=int, default=16,
                    help="Output H.264 CRF, lower = higher quality (default 16)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Detect and report crop region without writing video")
    args = ap.parse_args()

    if not args.video.exists():
        sys.exit(f"Video not found: {args.video}")
    if not have("ffmpeg"):
        sys.exit("ffmpeg not found on PATH.")

    cap = cv2.VideoCapture(str(args.video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    src_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    print(f"[crop] source: {args.video.name} {src_w}x{src_h} @ {src_fps:.2f} fps, "
          f"{src_frames} frames")

    print(f"[crop] sampling {args.samples} frames for face detection...")
    bboxes = sample_face_bboxes(args.video, args.samples)
    print(f"[crop] detected face in {len(bboxes)}/{args.samples} samples")

    if len(bboxes) < max(2, args.samples // 3):
        print(f"[crop] WARNING: detection rate is low ({len(bboxes)}/{args.samples}). "
              f"Crop region may be unreliable. Consider checking the source video.",
              file=sys.stderr)

    crop_x, crop_y, crop_size = compute_crop_box(
        bboxes, src_w, src_h, args.margin
    )
    coverage = 100.0 * crop_size / min(src_w, src_h)
    print(f"[crop] face bbox (median): {statistics.median([b[2] for b in bboxes])}x"
          f"{statistics.median([b[3] for b in bboxes])} px")
    print(f"[crop] crop region: {crop_size}x{crop_size} at ({crop_x},{crop_y}) "
          f"= {coverage:.0f}% of source height")
    print(f"[crop] output:     {args.target_size}x{args.target_size} "
          f"(upscale {args.target_size/crop_size:.2f}x)")

    if args.dry_run:
        print("[crop] dry run, not writing video.")
        return

    print(f"[crop] encoding {args.output}...")
    run_ffmpeg_crop(
        args.video, args.output,
        crop_x, crop_y, crop_size,
        args.target_size, args.crf,
    )
    print(f"[crop] done: {args.output}")


if __name__ == "__main__":
    main()

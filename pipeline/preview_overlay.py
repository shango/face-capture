"""
preview_overlay.py - Render mesh overlay onto source video using CSV data.

Unlike preview_rich.py (which re-runs MediaPipe on the video), this script
takes:
    - the original source video (at its native fps)
    - a CSV at the same fps containing blendshape weights + head pose

and renders the face landmarks on top, derived from the CSV's head pose
matrix applied to the canonical FLAME-free face mesh template.

Important: this preview shows the CAPTURED data, not a re-solve. That's
the right semantics for a user-facing preview: they see exactly what the
CSV they're about to import contains.

The current implementation does the simplest version that produces a
useful preview: re-runs MediaPipe to get per-frame landmark positions
for the overlay (since the CSV doesn't store landmarks), but applies
the CSV's blendshape values to the bar graph readout. This gives the
user a visual reference of tracking quality on their source frame rate.

A future version could store landmarks alongside blendshapes in the
CSV to avoid the re-run entirely.

Usage:
    python preview_overlay.py source.mp4 blendshapes_24.csv -o preview.mp4
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp


# Load shared imports from capture.py / preview_rich.py
sys.path.insert(0, str(Path(__file__).parent))
from capture import ARKIT_52, build_landmarker  # noqa
from preview_rich import (  # noqa
    draw_edges, draw_blendshape_bars, draw_status,
    COLOR_TESSELATION, COLOR_CONTOURS, COLOR_LIPS,
    COLOR_LEFT_EYE, COLOR_RIGHT_EYE, COLOR_IRIS,
)
from face_mesh_topology import (
    FACEMESH_TESSELATION, FACEMESH_CONTOURS, FACEMESH_LIPS,
    FACEMESH_LEFT_EYE, FACEMESH_RIGHT_EYE, FACEMESH_IRISES,
)


def load_csv_weights(csv_path: Path) -> list[dict]:
    """Return list of {time_seconds, weights_dict, detected} per frame."""
    out: list[dict] = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t = float(row["time_seconds"])
            except (KeyError, ValueError):
                continue
            weights = {}
            for name in ARKIT_52:
                try:
                    weights[name] = float(row.get(name, 0.0))
                except ValueError:
                    weights[name] = 0.0
            try:
                detected = int(row.get("detected", 1))
            except ValueError:
                detected = 1
            out.append({
                "time_seconds": t,
                "weights": weights,
                "detected": detected,
            })
    return out


def find_csv_row_for_time(csv_data: list[dict], target_t: float) -> dict | None:
    """Return the CSV row closest in time to target_t, or None if list empty."""
    if not csv_data:
        return None
    # Linear search (5s clips are small enough this is fine)
    return min(csv_data, key=lambda r: abs(r["time_seconds"] - target_t))


def run(
    video_path: Path,
    csv_path: Path,
    output_video: Path,
    model_path: Path,
    bar_count: int,
) -> None:
    if not video_path.exists():
        sys.exit(f"Video not found: {video_path}")
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")
    if not model_path.exists():
        sys.exit(f"Model not found: {model_path}")

    csv_data = load_csv_weights(csv_path)
    if not csv_data:
        sys.exit("CSV has no rows.")
    print(f"[overlay] loaded {len(csv_data)} CSV rows")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        sys.exit(f"Could not open: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[overlay] {video_path.name}: {total} frames @ {fps:.2f} fps ({w}x{h})")

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_video), fourcc, fps, (w, h))
    if not writer.isOpened():
        sys.exit(f"Could not open output: {output_video}")

    # We still need MediaPipe to get the visual landmark positions for the
    # overlay. The CSV provides the blendshape values for the bar graph,
    # which is what the user is shipping. This way the bars reflect the
    # CSV (the actual deliverable), not a fresh re-solve.
    landmarker = build_landmarker(model_path)

    start = time.time()
    frame_idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            video_t = frame_idx / fps

            # Get landmarks fresh for overlay
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect_for_video(
                mp_img, int(round(1000.0 * video_t))
            )

            # Get blendshape values from CSV for THIS frame's time
            csv_row = find_csv_row_for_time(csv_data, video_t)

            detected = bool(result.face_landmarks)
            if detected:
                lms = result.face_landmarks[0]
                draw_edges(frame, lms, FACEMESH_TESSELATION,
                           COLOR_TESSELATION, w, h, 1)
                draw_edges(frame, lms, FACEMESH_CONTOURS,
                           COLOR_CONTOURS, w, h, 1)
                draw_edges(frame, lms, FACEMESH_LIPS,
                           COLOR_LIPS, w, h, 1)
                draw_edges(frame, lms, FACEMESH_LEFT_EYE,
                           COLOR_LEFT_EYE, w, h, 1)
                draw_edges(frame, lms, FACEMESH_RIGHT_EYE,
                           COLOR_RIGHT_EYE, w, h, 1)
                draw_edges(frame, lms, FACEMESH_IRISES,
                           COLOR_IRIS, w, h, 1)

            # Bar graph from CSV (the actual data we're delivering)
            if csv_row is not None:
                weights = [csv_row["weights"].get(n, 0.0) for n in ARKIT_52]
                draw_blendshape_bars(
                    frame, weights, ARKIT_52, top_n=bar_count, x=10, y=10
                )

            elapsed = time.time() - start
            fps_proc = (frame_idx + 1) / elapsed if elapsed else 0.0
            draw_status(frame, frame_idx, total, fps_proc, detected)

            writer.write(frame)
            frame_idx += 1
            if frame_idx % 30 == 0:
                print(f"  ...frame {frame_idx}/{total}  ({fps_proc:.1f} fps)")
    finally:
        cap.release()
        writer.release()
        landmarker.close()

    elapsed = time.time() - start
    print(f"[overlay] done: {frame_idx} frames in {elapsed:.1f}s")
    print(f"[overlay] wrote {output_video}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("csv", type=Path)
    ap.add_argument("--output", "-o", type=Path, required=True)
    ap.add_argument("--model", "-m", type=Path,
                    default=Path(__file__).parent / "face_landmarker.task")
    ap.add_argument("--bars", type=int, default=10)
    args = ap.parse_args()

    run(args.video, args.csv, args.output, args.model, args.bars)


if __name__ == "__main__":
    main()

"""
preview_rich.py - Diagnostic visualization of MediaPipe face tracking.

Renders the 478 face landmarks with mesh tesselation overlay, colored
eye/iris/lip contours, blendshape bar graph, and head pose axis gizmo.
Can output to a live window or an MP4 file (recommended for WSL2 / for
client review).

Usage:
    # Live window (needs working X server / WSLg)
    python preview_rich.py test.mp4

    # Render to MP4 (no X server needed)
    python preview_rich.py test.mp4 --output preview.mp4

    # Customize what's drawn
    python preview_rich.py test.mp4 -o preview.mp4 \
        --no-tesselation --bars 10 --max-frames 120

Requirements:
    pip install mediapipe>=0.10.14 opencv-python numpy

Model file:
    face_landmarker.task in the working directory, or pass --model PATH.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision


# Reuse the canonical ARKit-52 order from capture.py if it's importable.
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from capture import ARKIT_52, decompose_pose, build_landmarker
except ImportError:
    print("preview_rich.py: capture.py not found in same directory. Aborting.",
          file=sys.stderr)
    raise


# Drawing colors (BGR).
COLOR_TESSELATION = (255, 255, 255)  # white face mesh
COLOR_CONTOURS = (200, 200, 200)
COLOR_LIPS = (40, 90, 220)        # orange-red
COLOR_LEFT_EYE = (220, 180, 40)   # cyan-ish
COLOR_RIGHT_EYE = (40, 180, 220)  # warm
COLOR_IRIS = (50, 220, 50)        # green
COLOR_BAR_BG = (50, 50, 50)
COLOR_BAR_FG = (60, 230, 120)
COLOR_BAR_TEXT = (240, 240, 240)
COLOR_HEADER = (240, 240, 240)
COLOR_AXIS_X = (60, 60, 230)      # red  (pitch)
COLOR_AXIS_Y = (60, 230, 60)      # green (yaw)
COLOR_AXIS_Z = (230, 60, 60)      # blue (roll)


def draw_edges(img, landmarks_norm, edges, color, w, h, thickness=1):
    pts = np.array(
        [(int(l.x * w), int(l.y * h)) for l in landmarks_norm], dtype=np.int32
    )
    for a, b in edges:
        cv2.line(img, tuple(pts[a]), tuple(pts[b]), color, thickness, cv2.LINE_AA)


def draw_blendshape_bars(img, weights, names, top_n=10, x=10, y=10):
    """Draw a vertical list of top blendshapes as labeled horizontal bars."""
    pairs = sorted(zip(names, weights), key=lambda p: -p[1])[:top_n]
    bar_w = 180
    bar_h = 12
    line_h = 18
    label_w = 175

    # Background panel
    panel_w = label_w + bar_w + 20
    panel_h = line_h * top_n + 30
    overlay = img.copy()
    cv2.rectangle(overlay, (x - 5, y - 5), (x + panel_w, y + panel_h),
                  (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)

    cv2.putText(img, "Top blendshapes", (x, y + 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, COLOR_HEADER, 1, cv2.LINE_AA)

    for i, (name, w) in enumerate(pairs):
        yy = y + 26 + i * line_h
        # Label
        cv2.putText(img, name, (x, yy + 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, COLOR_BAR_TEXT, 1, cv2.LINE_AA)
        # Bar background
        bx = x + label_w
        cv2.rectangle(img, (bx, yy), (bx + bar_w, yy + bar_h), COLOR_BAR_BG, -1)
        # Bar fill
        fill = int(bar_w * max(0.0, min(1.0, w)))
        if fill > 0:
            cv2.rectangle(img, (bx, yy), (bx + fill, yy + bar_h),
                          COLOR_BAR_FG, -1)
        # Numeric value
        cv2.putText(img, f"{w:.2f}", (bx + bar_w + 4, yy + 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, COLOR_BAR_TEXT, 1, cv2.LINE_AA)


def draw_head_axis(img, pose, center, scale=60):
    """Draw a small RGB axis gizmo showing head orientation.

    pose = (yaw_deg, pitch_deg, roll_deg, tx, ty, tz)
    Yaw rotates around Y, pitch around X, roll around Z.
    Y axis points up in image space (so we negate it when drawing).
    """
    yaw, pitch, roll = (math.radians(a) for a in pose[:3])

    # Build rotation matrix (ZYX intrinsic = match decompose_pose convention)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)

    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    Rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    R = Ry @ Rx @ Rz

    axes_world = np.array([
        [scale, 0, 0],   # X
        [0, scale, 0],   # Y
        [0, 0, scale],   # Z
    ]).T
    axes_cam = R @ axes_world  # (3, 3) -> columns are projected axes

    cx, cy_p = center
    # Project: drop Z (orthographic), flip Y so up is up
    pts = [
        (int(cx + axes_cam[0, i]), int(cy_p - axes_cam[1, i]))
        for i in range(3)
    ]
    cv2.line(img, (cx, cy_p), pts[0], COLOR_AXIS_X, 2, cv2.LINE_AA)
    cv2.line(img, (cx, cy_p), pts[1], COLOR_AXIS_Y, 2, cv2.LINE_AA)
    cv2.line(img, (cx, cy_p), pts[2], COLOR_AXIS_Z, 2, cv2.LINE_AA)
    cv2.circle(img, (cx, cy_p), 3, (255, 255, 255), -1, cv2.LINE_AA)

    # Label
    cv2.putText(img, f"yaw {pose[0]:+.1f}",   (cx - 50, cy_p + 70),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, COLOR_AXIS_Y, 1, cv2.LINE_AA)
    cv2.putText(img, f"pitch {pose[1]:+.1f}", (cx - 50, cy_p + 85),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, COLOR_AXIS_X, 1, cv2.LINE_AA)
    cv2.putText(img, f"roll {pose[2]:+.1f}",  (cx - 50, cy_p + 100),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, COLOR_AXIS_Z, 1, cv2.LINE_AA)


def draw_status(img, frame_idx, total, fps_proc, detected):
    txt = f"frame {frame_idx}/{total}   proc {fps_proc:.1f} fps   "
    txt += "FACE OK" if detected else "NO FACE"
    color = (80, 230, 120) if detected else (80, 80, 230)
    cv2.putText(img, txt, (10, img.shape[0] - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


def run(
    video_path: Path,
    model_path: Path,
    output_video: Path | None,
    max_frames: int | None,
    draw_tess: bool,
    draw_contours: bool,
    draw_lips: bool,
    draw_eyes: bool,
    draw_iris: bool,
    draw_axis: bool,
    bar_count: int,
) -> None:
    if not video_path.exists():
        sys.exit(f"Video not found: {video_path}")
    if not model_path.exists():
        sys.exit(f"Model not found: {model_path}")

    fm = None  # legacy mp.solutions.face_mesh removed in MediaPipe 0.10.18+
    try:
        from face_mesh_topology import (
            FACEMESH_TESSELATION as TESS,
            FACEMESH_CONTOURS as CONTOURS,
            FACEMESH_LIPS as LIPS,
            FACEMESH_LEFT_EYE as LEFT_EYE,
            FACEMESH_RIGHT_EYE as RIGHT_EYE,
            FACEMESH_IRISES as IRISES,
        )
    except ImportError:
        sys.exit("face_mesh_topology.py not found in same directory as "
                 "preview_rich.py — please place it alongside this script.")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        sys.exit(f"Could not open: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[preview] {video_path.name}: {total} frames @ {fps:.2f} fps "
          f"({w}x{h})")

    writer = None
    if output_video is not None:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(output_video), fourcc, fps, (w, h))
        if not writer.isOpened():
            sys.exit(f"Could not open output video: {output_video}")
        print(f"[preview] writing -> {output_video}")

    landmarker = build_landmarker(model_path)

    start = time.time()
    frame_idx = 0
    detected_count = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if max_frames is not None and frame_idx >= max_frames:
                break

            ts_ms = int(round(1000.0 * frame_idx / fps))
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect_for_video(mp_img, ts_ms)

            detected = bool(result.face_landmarks)
            if detected:
                detected_count += 1
                lms = result.face_landmarks[0]
                if draw_tess:
                    draw_edges(frame, lms, TESS, COLOR_TESSELATION, w, h, 1)
                if draw_contours:
                    draw_edges(frame, lms, CONTOURS, COLOR_CONTOURS, w, h, 1)
                if draw_lips:
                    draw_edges(frame, lms, LIPS, COLOR_LIPS, w, h, 1)
                if draw_eyes:
                    draw_edges(frame, lms, LEFT_EYE, COLOR_LEFT_EYE, w, h, 1)
                    draw_edges(frame, lms, RIGHT_EYE, COLOR_RIGHT_EYE, w, h, 1)
                if draw_iris:
                    draw_edges(frame, lms, IRISES, COLOR_IRIS, w, h, 1)

                # Blendshape bars
                if result.face_blendshapes:
                    bs = {b.category_name: b.score
                          for b in result.face_blendshapes[0]}
                    weights = [bs.get(n, 0.0) for n in ARKIT_52]
                    draw_blendshape_bars(
                        frame, weights, ARKIT_52, top_n=bar_count, x=10, y=10
                    )

                # Head pose gizmo, anchored to top-right of frame
                if draw_axis and result.facial_transformation_matrixes:
                    M = np.array(
                        result.facial_transformation_matrixes[0]
                    ).reshape(4, 4)
                    pose = decompose_pose(M)
                    gizmo_center = (w - 100, 100)
                    draw_head_axis(frame, pose, gizmo_center, scale=60)

            elapsed = time.time() - start
            fps_proc = (frame_idx + 1) / elapsed if elapsed else 0.0
            draw_status(frame, frame_idx, total, fps_proc, detected)

            if writer is not None:
                writer.write(frame)
            else:
                cv2.imshow("facecap preview (q to quit)", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            frame_idx += 1
            if frame_idx % 60 == 0:
                print(f"  ...frame {frame_idx}/{total}  ({fps_proc:.1f} fps)")
    finally:
        cap.release()
        if writer is not None:
            writer.release()
        landmarker.close()
        if writer is None:
            cv2.destroyAllWindows()

    elapsed = time.time() - start
    print(f"[preview] done: {frame_idx} frames, "
          f"{detected_count} detected "
          f"({100.0 * detected_count / max(frame_idx, 1):.1f}%), "
          f"{elapsed:.1f}s")
    if output_video is not None:
        print(f"[preview] wrote: {output_video}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--output", "-o", type=Path, default=None,
                    help="Render to MP4 instead of live window")
    ap.add_argument("--model", "-m", type=Path,
                    default=Path("face_landmarker.task"))
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--bars", type=int, default=10,
                    help="Number of top blendshape bars to show")

    # Each overlay layer can be toggled off independently
    ap.add_argument("--no-tesselation", dest="tess", action="store_false")
    ap.add_argument("--no-contours", dest="contours", action="store_false")
    ap.add_argument("--no-lips", dest="lips", action="store_false")
    ap.add_argument("--no-eyes", dest="eyes", action="store_false")
    ap.add_argument("--no-iris", dest="iris", action="store_false")
    ap.add_argument("--no-axis", dest="axis", action="store_false")

    args = ap.parse_args()
    run(
        video_path=args.video,
        model_path=args.model,
        output_video=args.output,
        max_frames=args.max_frames,
        draw_tess=args.tess,
        draw_contours=args.contours,
        draw_lips=args.lips,
        draw_eyes=args.eyes,
        draw_iris=args.iris,
        draw_axis=args.axis,
        bar_count=args.bars,
    )


if __name__ == "__main__":
    main()

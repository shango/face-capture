"""
capture.py - Extract ARKit-52 facial blendshapes from video using MediaPipe.

Usage:
    python capture.py <input_video> [--output out.csv] [--model face_landmarker.task]
                      [--preview] [--max-frames N]

Requirements:
    pip install mediapipe>=0.10.14 opencv-python numpy

Model file:
    Download once (3.7 MB) from:
    https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task

Output:
    CSV with columns: frame, time_seconds, <52 ARKit blendshape names>,
                      head_yaw, head_pitch, head_roll, head_tx, head_ty, head_tz

Notes:
    - Uses MediaPipe Tasks API in VIDEO mode for temporal consistency.
    - Head pose is decomposed from the facial_transformation_matrix into
      Euler angles (degrees) + translation (model units).
    - Frame timestamps are computed from the video FPS; if the source is VFR,
      consider re-muxing to CFR first with ffmpeg.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision


# Canonical ARKit-52 blendshape order, as emitted by MediaPipe Face Landmarker.
# Index 0 is "_neutral" which we drop from CSV output (it's not an animatable target).
ARKIT_52 = [
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight",
    "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight",
    "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight",
    "mouthFrownLeft", "mouthFrownRight",
    "mouthFunnel",
    "mouthLeft",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight",
    "mouthPucker",
    "mouthRight",
    "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "noseSneerLeft", "noseSneerRight",
    "tongueOut",
]


def decompose_pose(matrix_4x4: np.ndarray) -> tuple[float, float, float, float, float, float]:
    """Return (yaw_deg, pitch_deg, roll_deg, tx, ty, tz) from a 4x4 transform.

    Uses the ZYX (yaw-pitch-roll) intrinsic Euler convention, which matches
    Maya's default rotate order XYZ when applied in the typical head-rig sense.
    Adjust to suit downstream conventions.
    """
    R = matrix_4x4[:3, :3]
    t = matrix_4x4[:3, 3]

    # Pitch (X), Yaw (Y), Roll (Z) extraction.
    sy = math.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2)
    singular = sy < 1e-6
    if not singular:
        pitch = math.atan2(-R[2, 0], sy)
        yaw = math.atan2(R[1, 0], R[0, 0])
        roll = math.atan2(R[2, 1], R[2, 2])
    else:
        pitch = math.atan2(-R[2, 0], sy)
        yaw = 0.0
        roll = math.atan2(-R[1, 2], R[1, 1])

    return (
        math.degrees(yaw),
        math.degrees(pitch),
        math.degrees(roll),
        float(t[0]), float(t[1]), float(t[2]),
    )


def build_landmarker(model_path: Path) -> mp_vision.FaceLandmarker:
    base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.VIDEO,
        num_faces=1,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    return mp_vision.FaceLandmarker.create_from_options(options)


def process_video(
    video_path: Path,
    model_path: Path,
    output_csv: Path,
    preview: bool = False,
    max_frames: int | None = None,
) -> None:
    if not video_path.exists():
        sys.exit(f"Video not found: {video_path}")
    if not model_path.exists():
        sys.exit(
            f"Model not found: {model_path}\n"
            f"Download with:\n"
            f"  wget https://storage.googleapis.com/mediapipe-models/"
            f"face_landmarker/face_landmarker/float16/1/face_landmarker.task"
        )

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        sys.exit(f"OpenCV could not open: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[capture] {video_path.name}: {total} frames @ {fps:.2f} fps")

    landmarker = build_landmarker(model_path)

    header = (
        ["frame", "time_seconds"]
        + ARKIT_52
        + ["head_yaw", "head_pitch", "head_roll", "head_tx", "head_ty", "head_tz"]
        + ["detected"]
    )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    f = output_csv.open("w", newline="")
    writer = csv.writer(f)
    writer.writerow(header)

    frame_idx = 0
    detected_count = 0
    start = time.time()
    try:
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                break
            if max_frames is not None and frame_idx >= max_frames:
                break

            ts_ms = int(round(1000.0 * frame_idx / fps))
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)

            result = landmarker.detect_for_video(mp_image, ts_ms)

            row_weights = [0.0] * len(ARKIT_52)
            pose = (0.0,) * 6
            detected = 0

            if result.face_blendshapes:
                detected = 1
                detected_count += 1
                # result.face_blendshapes[0] is a list of Category objects including "_neutral"
                blendshapes = {b.category_name: b.score for b in result.face_blendshapes[0]}
                for i, name in enumerate(ARKIT_52):
                    row_weights[i] = float(blendshapes.get(name, 0.0))

                if result.facial_transformation_matrixes:
                    M = np.array(result.facial_transformation_matrixes[0]).reshape(4, 4)
                    pose = decompose_pose(M)

            writer.writerow(
                [frame_idx, ts_ms / 1000.0] + row_weights + list(pose) + [detected]
            )

            if preview and result.face_blendshapes:
                # Overlay top-5 active blendshapes for sanity-check.
                top = sorted(
                    [(n, w) for n, w in zip(ARKIT_52, row_weights)],
                    key=lambda p: -p[1],
                )[:5]
                y = 20
                for name, w in top:
                    cv2.putText(
                        frame_bgr, f"{name}: {w:.2f}", (10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1
                    )
                    y += 18
                cv2.imshow("facecap (q to quit)", frame_bgr)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            frame_idx += 1
            if frame_idx % 30 == 0:
                elapsed = time.time() - start
                rate = frame_idx / elapsed if elapsed else 0
                print(f"  ...frame {frame_idx}/{total}  ({rate:.1f} fps processing)")
    finally:
        cap.release()
        f.close()
        landmarker.close()
        if preview:
            cv2.destroyAllWindows()

    elapsed = time.time() - start
    print(
        f"[capture] done: {frame_idx} frames, "
        f"{detected_count} with face detected "
        f"({100.0 * detected_count / max(frame_idx, 1):.1f}%), "
        f"{elapsed:.1f}s elapsed"
    )
    print(f"[capture] wrote: {output_csv}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path, help="Input video file")
    ap.add_argument("--output", "-o", type=Path, default=Path("blendshapes.csv"))
    ap.add_argument("--model", "-m", type=Path,
                    default=Path(__file__).parent / "face_landmarker.task")
    ap.add_argument("--preview", action="store_true",
                    help="Show realtime overlay of top blendshapes")
    ap.add_argument("--max-frames", type=int, default=None,
                    help="Stop after N frames (for quick testing)")
    args = ap.parse_args()

    process_video(args.video, args.model, args.output, args.preview, args.max_frames)


if __name__ == "__main__":
    main()

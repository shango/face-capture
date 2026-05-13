"""
smooth.py - Apply a One Euro filter to a blendshape/pose CSV.

The One Euro filter (Casiez et al., 2012) is a low-pass filter with an
adaptive cutoff: more smoothing when the signal is slow (idle face),
less smoothing when the signal is fast (a real expression change).
This is the right choice for face tracking output because it preserves
fast motion onsets while killing low-amplitude jitter.

Reads a CSV from capture.py, applies One Euro per-column, writes the
smoothed CSV in the same format. Pose columns and frame/time pass
through with their own (gentler) filter settings.

Usage:
    python smooth.py blendshapes.csv -o blendshapes_smoothed.csv
    python smooth.py blendshapes.csv -o out.csv --min-cutoff 1.5 --beta 0.05
    python smooth.py blendshapes.csv -o out.csv --no-pose  # skip filtering pose

Defaults are tuned for 24fps face capture; for 60fps+ raise --min-cutoff
to ~3.0 to reduce phase lag.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


POSE_COLS = ["head_yaw", "head_pitch", "head_roll", "head_tx", "head_ty", "head_tz"]
PASSTHROUGH_COLS = {"frame", "time_seconds", "detected"}


class OneEuroFilter:
    """Single-channel One Euro filter.

    Args:
        min_cutoff: Hz. Minimum cutoff frequency. Lower = more smoothing of slow signals.
        beta: speed coefficient. Higher = faster adaptation to fast motion.
        d_cutoff: Hz. Cutoff for the derivative estimator. 1.0 is standard.
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0,
                 d_cutoff: float = 1.0):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._x_prev: float | None = None
        self._dx_prev: float = 0.0
        self._t_prev: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        # Standard exponential-smoothing alpha given a cutoff frequency.
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, t: float, x: float) -> float:
        if self._t_prev is None or self._x_prev is None:
            self._t_prev = t
            self._x_prev = x
            self._dx_prev = 0.0
            return x

        dt = t - self._t_prev
        if dt <= 0:
            return self._x_prev

        # Derivative, low-pass filtered with d_cutoff
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self._dx_prev

        # Adaptive cutoff based on derivative magnitude
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self._x_prev

        self._t_prev = t
        self._x_prev = x_hat
        self._dx_prev = dx_hat
        return x_hat


def smooth_csv(
    input_csv: Path,
    output_csv: Path,
    min_cutoff: float = 1.5,
    beta: float = 0.02,
    pose_min_cutoff: float = 2.5,
    pose_beta: float = 0.01,
    smooth_pose: bool = True,
) -> None:
    with input_csv.open("r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames or []

    if not rows:
        raise SystemExit("Input CSV is empty.")

    # Identify column categories
    pose_cols = [c for c in POSE_COLS if c in header]
    blend_cols = [
        c for c in header
        if c not in PASSTHROUGH_COLS and c not in POSE_COLS
    ]

    # Build a filter per channel
    blend_filters = {
        c: OneEuroFilter(min_cutoff=min_cutoff, beta=beta) for c in blend_cols
    }
    pose_filters = {
        c: OneEuroFilter(min_cutoff=pose_min_cutoff, beta=pose_beta)
        for c in pose_cols
    } if smooth_pose else {}

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="") as g:
        writer = csv.DictWriter(g, fieldnames=header)
        writer.writeheader()
        for row in rows:
            try:
                t = float(row["time_seconds"])
            except (KeyError, ValueError):
                # If no time column, fall back to constant dt
                t = float(row.get("frame", 0)) / 24.0

            # Don't smooth across detection gaps: if face wasn't detected,
            # zero-out and reset the filters so the next detection starts fresh.
            try:
                detected = int(row.get("detected", 1))
            except ValueError:
                detected = 1

            out = dict(row)
            if not detected:
                for c in blend_cols:
                    out[c] = "0.0"
                    blend_filters[c] = OneEuroFilter(
                        min_cutoff=min_cutoff, beta=beta
                    )
                for c in pose_cols:
                    if smooth_pose:
                        out[c] = "0.0"
                        pose_filters[c] = OneEuroFilter(
                            min_cutoff=pose_min_cutoff, beta=pose_beta
                        )
                writer.writerow(out)
                continue

            for c in blend_cols:
                try:
                    v = float(row[c])
                except ValueError:
                    v = 0.0
                v_hat = blend_filters[c](t, v)
                # Clamp to [0,1] post-filter (filter can overshoot slightly)
                out[c] = f"{max(0.0, min(1.0, v_hat)):.6f}"

            if smooth_pose:
                for c in pose_cols:
                    try:
                        v = float(row[c])
                    except ValueError:
                        v = 0.0
                    out[c] = f"{pose_filters[c](t, v):.6f}"

            writer.writerow(out)

    print(f"[smooth] wrote {len(rows)} frames -> {output_csv}")
    print(f"[smooth] blendshapes: min_cutoff={min_cutoff}, beta={beta}")
    if smooth_pose:
        print(f"[smooth] pose:        min_cutoff={pose_min_cutoff}, "
              f"beta={pose_beta}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv", type=Path)
    ap.add_argument("--output", "-o", type=Path, default=Path("smoothed.csv"))
    ap.add_argument("--min-cutoff", type=float, default=1.5,
                    help="Min cutoff Hz for blendshapes (default 1.5; "
                         "raise to 3.0 for 60fps+)")
    ap.add_argument("--beta", type=float, default=0.02,
                    help="Speed coefficient for blendshapes (default 0.02)")
    ap.add_argument("--pose-min-cutoff", type=float, default=2.5)
    ap.add_argument("--pose-beta", type=float, default=0.01)
    ap.add_argument("--no-pose", dest="smooth_pose", action="store_false",
                    help="Don't smooth head pose columns")
    args = ap.parse_args()

    smooth_csv(
        args.input_csv, args.output,
        min_cutoff=args.min_cutoff, beta=args.beta,
        pose_min_cutoff=args.pose_min_cutoff, pose_beta=args.pose_beta,
        smooth_pose=args.smooth_pose,
    )


if __name__ == "__main__":
    main()

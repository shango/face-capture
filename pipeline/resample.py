"""
resample.py - Resample a blendshape/pose CSV to a different frame rate.

Used to take the high-density CSV from running capture on an interpolated
60fps video and produce a 24fps CSV that matches the original source
timing for delivery to the user.

Resampling is linear per channel against the time_seconds column, which is
the authoritative timing axis. The frame column is regenerated for the
new rate.

Usage:
    python resample.py blendshapes_60.csv -o blendshapes_24.csv --target-fps 24
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


PASSTHROUGH_COLS = {"frame", "time_seconds", "detected"}


def _lerp(t: float, t0: float, t1: float, v0: float, v1: float) -> float:
    if t1 <= t0:
        return v0
    a = (t - t0) / (t1 - t0)
    return v0 + a * (v1 - v0)


def resample_csv(input_csv: Path, output_csv: Path, target_fps: float) -> None:
    with input_csv.open("r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames or []

    if not rows:
        sys.exit("Input CSV is empty.")

    # Parse times and values
    times: list[float] = []
    parsed_rows: list[dict[str, float]] = []
    for row in rows:
        try:
            t = float(row["time_seconds"])
        except (KeyError, ValueError):
            continue
        times.append(t)
        parsed: dict[str, float] = {}
        for col in header:
            if col in ("frame", "time_seconds"):
                continue
            try:
                parsed[col] = float(row[col])
            except (ValueError, KeyError):
                parsed[col] = 0.0
        parsed_rows.append(parsed)

    if len(times) < 2:
        sys.exit("Need at least 2 input rows to resample.")

    t_start = times[0]
    t_end = times[-1]
    dt_out = 1.0 / target_fps

    # Build output time samples
    out_times: list[float] = []
    t = t_start
    out_frame = 0
    while t <= t_end + 1e-9:
        out_times.append(t)
        t = t_start + (out_frame + 1) * dt_out
        out_frame += 1

    # For each output sample, find bracket in input times and lerp
    data_cols = [c for c in header if c not in ("frame", "time_seconds")]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="") as g:
        writer = csv.writer(g)
        writer.writerow(header)

        in_idx = 0
        for out_i, out_t in enumerate(out_times):
            # Advance in_idx so that times[in_idx] <= out_t <= times[in_idx+1]
            while (in_idx + 1 < len(times) - 1
                   and times[in_idx + 1] < out_t):
                in_idx += 1

            i0 = in_idx
            i1 = min(in_idx + 1, len(times) - 1)
            t0, t1 = times[i0], times[i1]

            out_vals: dict[str, float] = {}
            for col in data_cols:
                v0 = parsed_rows[i0].get(col, 0.0)
                v1 = parsed_rows[i1].get(col, 0.0)
                # `detected` is a 0/1 flag; nearest-neighbor instead of lerp
                if col == "detected":
                    out_vals[col] = v0 if (out_t - t0) <= (t1 - out_t) else v1
                else:
                    out_vals[col] = _lerp(out_t, t0, t1, v0, v1)

            row_out = [out_i, f"{out_t:.6f}"]
            for col in data_cols:
                v = out_vals[col]
                if col == "detected":
                    row_out.append(int(round(v)))
                else:
                    row_out.append(f"{v:.6f}")
            writer.writerow(row_out)

    dt = times[1] - times[0] if len(times) >= 2 else 0.0
    in_fps = f"{1 / dt:.2f}" if dt > 0 else "?"
    print(f"[resample] {len(rows)} rows ({in_fps} fps) "
          f"-> {len(out_times)} rows ({target_fps:.2f} fps)")
    print(f"[resample] wrote {output_csv}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv", type=Path)
    ap.add_argument("--output", "-o", type=Path, required=True)
    ap.add_argument("--target-fps", type=float, required=True)
    args = ap.parse_args()

    if not args.input_csv.exists():
        sys.exit(f"Input not found: {args.input_csv}")
    if args.target_fps <= 0:
        sys.exit("--target-fps must be positive")

    resample_csv(args.input_csv, args.output, args.target_fps)


if __name__ == "__main__":
    main()

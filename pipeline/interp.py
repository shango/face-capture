"""
interp.py - Interpolate a video to a higher frame rate before face capture.

Wraps a frame interpolator to take 24fps input and produce 48/60/96fps output
that gives MediaPipe denser temporal samples to solve from. Critical for
clips where reshooting at a higher native frame rate isn't an option.

This script does NOT install the interpolator itself - it expects the
`rife-ncnn-vulkan` binary to be on PATH or specified via --bin. That tool
is the right choice for AMD GPUs (uses Vulkan, not CUDA/ROCm) and runs on
Windows, Linux, and WSL2 with a working Vulkan loader.

Install (one-time):
    Download a release zip from:
      https://github.com/nihui/rife-ncnn-vulkan/releases
    Extract to e.g. ~/tools/rife-ncnn-vulkan/ and add to PATH, or pass
    --bin ~/tools/rife-ncnn-vulkan/rife-ncnn-vulkan

    On WSL2 with AMD: Vulkan must be reachable. Test with `vulkaninfo | head`.
    If it doesn't work in WSL, run the Windows .exe on the Windows side and
    bring the output mp4 back into WSL.

Usage:
    # Double frame rate (24 -> 48)
    python interp.py test.mp4 -o test_48.mp4

    # 24 -> 96fps (4x)
    python interp.py test.mp4 -o test_96.mp4 --multiplier 4

    # CPU fallback (no GPU): use ffmpeg's minterpolate
    python interp.py test.mp4 -o test_60.mp4 --backend ffmpeg --target-fps 60

Pipeline integration:
    python interp.py test.mp4 -o test_interp.mp4 --multiplier 2
    python capture.py test_interp.mp4 -o blendshapes.csv
    python smooth.py blendshapes.csv -o blendshapes_smoothed.csv
    python retarget.py blendshapes_smoothed.csv mapping.json -o retargeted.csv

Requirements:
    ffmpeg on PATH (for frame extraction / encoding / and the ffmpeg backend).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def get_source_fps(video: Path) -> float:
    """Use ffprobe to read the average frame rate."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # avg_frame_rate is "num/den"
    if "/" in out:
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 24.0
    return float(out)


def interpolate_rife(
    src: Path, dst: Path, multiplier: int, rife_bin: str, model: str | None,
) -> None:
    """Two-step: extract frames -> interpolate -> reassemble at new fps."""
    if not have(rife_bin):
        sys.exit(
            f"'{rife_bin}' not found on PATH.\n"
            f"Install rife-ncnn-vulkan from:\n"
            f"  https://github.com/nihui/rife-ncnn-vulkan/releases\n"
            f"Then pass --bin /path/to/rife-ncnn-vulkan, or add it to PATH."
        )
    if not have("ffmpeg") or not have("ffprobe"):
        sys.exit("ffmpeg/ffprobe required on PATH.")

    src_fps = get_source_fps(src)
    target_fps = src_fps * multiplier
    print(f"[interp] {src.name}: {src_fps:.2f} fps -> {target_fps:.2f} fps "
          f"(rife x{multiplier})")

    with tempfile.TemporaryDirectory(prefix="rife_") as td:
        td_path = Path(td)
        in_dir = td_path / "in"
        out_dir = td_path / "out"
        in_dir.mkdir()
        out_dir.mkdir()

        # 1) Extract frames as png
        print("[interp] extracting frames...")
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
             "-vsync", "0", str(in_dir / "frame_%08d.png")],
            check=True,
        )
        frame_count = len(list(in_dir.glob("frame_*.png")))
        print(f"[interp] extracted {frame_count} frames")

        # 2) RIFE interpolate. rife-ncnn-vulkan -n N controls total output frames.
        target_total = frame_count * multiplier
        rife_cmd = [
            rife_bin, "-i", str(in_dir), "-o", str(out_dir),
            "-n", str(target_total),
        ]
        if model:
            rife_cmd += ["-m", model]
        print(f"[interp] running RIFE -> {target_total} frames "
              f"(this may take a while on integrated GPU)")
        subprocess.run(rife_cmd, check=True)

        out_count = len(list(out_dir.glob("*.png")))
        print(f"[interp] RIFE produced {out_count} frames")

        # 3) Reassemble at new fps
        # Pull audio from source if present (face-only clips usually have it)
        print("[interp] encoding output video...")
        # First try with audio, fall back without
        encode_cmd = [
            "ffmpeg", "-loglevel", "error", "-y",
            "-framerate", f"{target_fps}",
            "-i", str(out_dir / "%08d.png"),
            "-i", str(src),
            "-map", "0:v", "-map", "1:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "16", "-preset", "medium",
            "-c:a", "aac", "-shortest",
            str(dst),
        ]
        result = subprocess.run(encode_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Retry video-only
            encode_cmd_noaudio = [
                "ffmpeg", "-loglevel", "error", "-y",
                "-framerate", f"{target_fps}",
                "-i", str(out_dir / "%08d.png"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-crf", "16", "-preset", "medium",
                str(dst),
            ]
            subprocess.run(encode_cmd_noaudio, check=True)

    print(f"[interp] wrote: {dst}")


def interpolate_ffmpeg(src: Path, dst: Path, target_fps: float) -> None:
    """CPU-only fallback using ffmpeg's minterpolate filter.

    Lower quality than RIFE but requires no extra setup. Use when you can't
    get RIFE working on this machine.
    """
    if not have("ffmpeg"):
        sys.exit("ffmpeg required on PATH.")

    src_fps = get_source_fps(src)
    print(f"[interp] {src.name}: {src_fps:.2f} fps -> {target_fps:.2f} fps "
          f"(ffmpeg minterpolate, CPU)")

    # mci (motion compensated interpolation) with bidirectional ME is the
    # highest-quality mode of minterpolate. Slow but acceptable for short clips.
    vf = (f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:"
          f"me_mode=bidir:vsbmc=1")
    subprocess.run(
        ["ffmpeg", "-loglevel", "info", "-y", "-i", str(src),
         "-vf", vf,
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-crf", "16", "-preset", "medium",
         "-c:a", "copy",
         str(dst)],
        check=True,
    )
    print(f"[interp] wrote: {dst}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--output", "-o", type=Path, required=True)
    ap.add_argument("--backend", choices=["rife", "ffmpeg"], default="rife",
                    help="rife = neural interpolation (better, needs binary). "
                         "ffmpeg = CPU minterpolate (worse, no setup).")
    ap.add_argument("--multiplier", type=int, default=2,
                    help="Integer fps multiplier for RIFE backend (2, 4, 8)")
    ap.add_argument("--target-fps", type=float, default=60.0,
                    help="Target fps for ffmpeg backend (default 60)")
    ap.add_argument("--bin", default="rife-ncnn-vulkan",
                    help="Path to rife-ncnn-vulkan binary")
    ap.add_argument("--model", default=None,
                    help="RIFE model name, e.g. 'rife-v4.6' (binary's default if unset)")
    args = ap.parse_args()

    if not args.video.exists():
        sys.exit(f"Video not found: {args.video}")

    if args.backend == "rife":
        if args.multiplier not in (2, 4, 8):
            sys.exit("--multiplier must be 2, 4, or 8 for RIFE.")
        interpolate_rife(args.video, args.output, args.multiplier,
                         args.bin, args.model)
    else:
        interpolate_ffmpeg(args.video, args.output, args.target_fps)


if __name__ == "__main__":
    main()

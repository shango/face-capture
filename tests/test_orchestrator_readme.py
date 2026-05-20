"""Tests for the conditional USD section in the bundle README template."""

from __future__ import annotations

from pathlib import Path

from pipeline.orchestrator import _write_readme


def _render(tmp_path: Path, *, with_usd: bool) -> str:
    target = _write_readme(
        out_dir=tmp_path,
        csv_filename="clip_blendshapes.csv",
        source_filename="clip.mp4",
        n_frames=120,
        readme_name="README.txt",
        preview_name="clip_preview.mp4",
        maya_name="clip_apply_in_maya.py",
        blender_name="clip_apply_in_blender.py",
        usd_head_name="clip_arkit_head.usdc" if with_usd else None,
        usd_animated_name="clip_animated.usda" if with_usd else None,
    )
    return target.read_text()


def test_readme_omits_usd_section_when_assets_absent(tmp_path: Path) -> None:
    text = _render(tmp_path, with_usd=False)
    # Standard parts present.
    assert "clip_blendshapes.csv" in text
    assert "clip_apply_in_maya.py" in text
    assert "clip_apply_in_blender.py" in text
    # USD parts MUST NOT appear — otherwise we'd be telling the user to open
    # files that aren't in their bundle.
    assert ".usdc" not in text
    assert ".usda" not in text
    assert "Quick play (USD" not in text


def test_readme_includes_usd_section_when_assets_present(tmp_path: Path) -> None:
    text = _render(tmp_path, with_usd=True)
    assert "clip_arkit_head.usdc" in text
    assert "clip_animated.usda" in text
    assert "Quick play (USD" in text
    # Cross-DCC instructions all named.
    for dcc in ("Maya 2022+", "Blender 3.5+", "Houdini", "Omniverse", "Unreal"):
        assert dcc in text
    # Sublayer warning is there — losing it would silently break bundles
    # where users move only the .usda.
    assert "same folder" in text

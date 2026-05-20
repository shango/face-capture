"""Tests for pipeline.usd_export.bake_animated_usd.

We synthesise a minimal head USD in code (3 blendshape targets bound to a
single-tri Mesh via UsdSkelBindingAPI), pair it with a tiny CSV, run the
bake, then re-open the result to assert the SkelAnimation has the expected
shape names and per-frame weight time samples.

These tests don't depend on Maya or on the real arkit_head.usdc — they only
need usd-core, which is a hard dependency of the project.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom, UsdSkel, Vt

from pipeline.usd_export import bake_animated_usd


def _make_synthetic_head_usd(out_path: Path, blendshape_names: list[str]) -> None:
    """Write a minimal SkelBindingAPI-applied mesh with N blendshape targets."""
    stage = Usd.Stage.CreateNew(str(out_path))

    # Root xform → mesh + per-target BlendShape children.
    root = UsdGeom.Xform.Define(stage, "/Head")
    stage.SetDefaultPrim(root.GetPrim())

    mesh = UsdGeom.Mesh.Define(stage, "/Head/head_mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])

    # Apply SkelBindingAPI on the mesh.
    binding = UsdSkel.BindingAPI.Apply(mesh.GetPrim())
    binding.CreateBlendShapesAttr(Vt.TokenArray(blendshape_names))

    target_paths = []
    for name in blendshape_names:
        bs = UsdSkel.BlendShape.Define(stage, f"/Head/head_mesh/{name}")
        # Zero offsets are valid — we don't need actual geometry deltas for
        # the bake tests; we're verifying the animation authoring, not
        # deformation.
        bs.CreateOffsetsAttr(Vt.Vec3fArray([(0, 0, 0)] * 3))
        bs.CreatePointIndicesAttr(Vt.IntArray([0, 1, 2]))
        target_paths.append(bs.GetPath())
    binding.CreateBlendShapeTargetsRel().SetTargets(target_paths)

    stage.GetRootLayer().Save()


def _write_csv(out_path: Path, columns: list[str], rows: list[list[float]]) -> None:
    """Write a CSV with the standard frame/time_seconds/<cols>/pose/detected schema."""
    pose_cols = ["head_yaw", "head_pitch", "head_roll", "head_tx", "head_ty", "head_tz"]
    header = ["frame", "time_seconds"] + columns + pose_cols + ["detected"]
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i, row in enumerate(rows):
            w.writerow(
                [i, i / 24.0] + row + [0.0] * 6 + [1]
            )


def test_bake_writes_animation_with_matching_blendshapes(tmp_path: Path) -> None:
    head = tmp_path / "arkit_head.usdc"
    _make_synthetic_head_usd(head, ["jawOpen", "eyeBlinkLeft", "browInnerUp"])

    csv_path = tmp_path / "blendshapes.csv"
    _write_csv(
        csv_path,
        columns=["jawOpen", "eyeBlinkLeft", "browInnerUp"],
        rows=[
            [0.0, 0.0, 0.0],
            [0.25, 0.5, 0.1],
            [0.5, 1.0, 0.2],
            [0.25, 0.5, 0.1],
            [0.0, 0.0, 0.0],
        ],
    )

    out = tmp_path / "clip_animated.usda"
    result = bake_animated_usd(csv_path, head, out, fps=24.0)

    assert out.is_file()
    assert result.frame_count == 5
    assert result.rig_blendshape_count == 3
    assert set(result.matched_columns) == {"jawOpen", "eyeBlinkLeft", "browInnerUp"}
    assert result.unmatched_columns == ()

    # Re-open and verify.
    stage = Usd.Stage.Open(str(out))
    assert stage.GetStartTimeCode() == 0.0
    assert stage.GetEndTimeCode() == 4.0
    assert stage.GetTimeCodesPerSecond() == 24.0

    # The SkelAnimation should now exist on the composed stage.
    anims = [p for p in stage.Traverse() if p.IsA(UsdSkel.Animation)]
    assert len(anims) == 1
    anim = UsdSkel.Animation(anims[0])

    names = list(anim.GetBlendShapesAttr().Get() or [])
    assert names == ["jawOpen", "eyeBlinkLeft", "browInnerUp"]

    weights_attr = anim.GetBlendShapeWeightsAttr()
    sample_times = weights_attr.GetTimeSamples()
    assert sample_times == [0.0, 1.0, 2.0, 3.0, 4.0]

    # Middle frame should match what we wrote.
    middle = list(weights_attr.Get(Usd.TimeCode(2.0)))
    assert middle == pytest.approx([0.5, 1.0, 0.2])


def test_bake_warns_on_unmatched_csv_columns(tmp_path: Path, caplog) -> None:
    head = tmp_path / "arkit_head.usdc"
    # Rig has only jawOpen — CSV will have an extra column.
    _make_synthetic_head_usd(head, ["jawOpen"])

    csv_path = tmp_path / "blendshapes.csv"
    _write_csv(
        csv_path,
        columns=["jawOpen", "tongueOut"],  # tongueOut not on the rig
        rows=[[0.5, 0.9]],
    )

    out = tmp_path / "clip_animated.usda"
    with caplog.at_level("WARNING", logger="pipeline.usd_export"):
        result = bake_animated_usd(csv_path, head, out, fps=24.0)

    assert result.matched_columns == ("jawOpen",)
    assert result.unmatched_columns == ("tongueOut",)
    assert any("tongueOut" in r.getMessage() for r in caplog.records)


def test_bake_is_case_insensitive(tmp_path: Path) -> None:
    head = tmp_path / "arkit_head.usdc"
    _make_synthetic_head_usd(head, ["JawOpen"])  # mixed case on rig

    csv_path = tmp_path / "blendshapes.csv"
    _write_csv(csv_path, columns=["jawopen"], rows=[[0.7]])  # lowercase in CSV

    result = bake_animated_usd(csv_path, head, tmp_path / "clip_animated.usda")
    assert result.matched_columns == ("jawopen",)
    assert result.unmatched_columns == ()

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
from pxr import Gf, Sdf, Usd, UsdGeom, UsdSkel, Vt

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


def _make_skeletal_head_usd(out_path: Path, blendshape_names: list[str]) -> None:
    """Write a head that mirrors mayaUSDExport: SkelRoot > Skeleton sibling mesh.

    This is the structure that exposed the binding bug — UsdSkel resolves a
    skeleton's animation from `skel:animationSource` at the *skeleton* prim, so
    a bake that bound the anim on the sibling mesh left the skeleton (and thus
    the deformed mesh) motionless even though the weight samples were present.
    """
    stage = Usd.Stage.CreateNew(str(out_path))
    root = UsdSkel.Root.Define(stage, "/Head")
    stage.SetDefaultPrim(root.GetPrim())

    skel = UsdSkel.Skeleton.Define(stage, "/Head/head_mesh_Skeleton")
    skel.CreateJointsAttr(Vt.TokenArray(["root"]))
    skel.CreateBindTransformsAttr(Vt.Matrix4dArray([Gf.Matrix4d(1.0)]))
    skel.CreateRestTransformsAttr(Vt.Matrix4dArray([Gf.Matrix4d(1.0)]))

    mesh = UsdGeom.Mesh.Define(stage, "/Head/head_mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])

    binding = UsdSkel.BindingAPI.Apply(mesh.GetPrim())
    binding.CreateSkeletonRel().SetTargets([skel.GetPath()])
    binding.CreateBlendShapesAttr(Vt.TokenArray(blendshape_names))
    target_paths = []
    for name in blendshape_names:
        bs = UsdSkel.BlendShape.Define(stage, f"/Head/head_mesh/{name}")
        bs.CreateOffsetsAttr(Vt.Vec3fArray([(0, 0, 0)] * 3))
        bs.CreatePointIndicesAttr(Vt.IntArray([0, 1, 2]))
        target_paths.append(bs.GetPath())
    binding.CreateBlendShapeTargetsRel().SetTargets(target_paths)

    stage.GetRootLayer().Save()


def test_bake_binds_animation_so_skeleton_resolves_it(tmp_path: Path) -> None:
    """The baked animation must actually drive the skeleton, not just exist.

    Regression: the anim was bound on the sibling mesh, so UsdSkelCache
    resolved no animation for the skeleton and the head exported motionless.
    """
    head = tmp_path / "arkit_head.usdc"
    _make_skeletal_head_usd(head, ["jawOpen", "eyeBlinkLeft"])

    csv_path = tmp_path / "blendshapes.csv"
    _write_csv(
        csv_path,
        columns=["jawOpen", "eyeBlinkLeft"],
        rows=[[0.0, 0.0], [0.5, 0.25], [1.0, 0.5]],
    )

    out = tmp_path / "clip_animated.usda"
    bake_animated_usd(csv_path, head, out, fps=24.0)

    stage = Usd.Stage.Open(str(out))
    cache = UsdSkel.Cache()
    root = UsdSkel.Root(stage.GetDefaultPrim())
    cache.Populate(root, Usd.TraverseInstanceProxies())
    bindings = cache.ComputeSkelBindings(root, Usd.TraverseInstanceProxies())
    assert len(bindings) == 1

    skel_query = cache.GetSkelQuery(bindings[0].GetSkeleton())
    anim_query = skel_query.GetAnimQuery()
    # An unbound skeleton yields an *invalid* (falsy) query, not None — so check
    # validity, not `is not None`. This is the assertion that fails on the bug.
    assert bool(anim_query), "animation did not resolve onto the skeleton"

    # And the resolved weights are the ones we baked (frame 2 → [1.0, 0.5]).
    weights = anim_query.ComputeBlendShapeWeights(Usd.TimeCode(2.0))
    assert list(weights) == pytest.approx([1.0, 0.5])


def test_bake_is_case_insensitive(tmp_path: Path) -> None:
    head = tmp_path / "arkit_head.usdc"
    _make_synthetic_head_usd(head, ["JawOpen"])  # mixed case on rig

    csv_path = tmp_path / "blendshapes.csv"
    _write_csv(csv_path, columns=["jawopen"], rows=[[0.7]])  # lowercase in CSV

    result = bake_animated_usd(csv_path, head, tmp_path / "clip_animated.usda")
    assert result.matched_columns == ("jawopen",)
    assert result.unmatched_columns == ()

"""usd_export.py — bake a blendshape-animation CSV into a USD layer.

`bake_animated_usd()` reads:
  - a CSV produced by the capture pipeline (24fps ARKit-52, see capture.py)
  - the static head asset exported once from Maya (see
    scripts/export_arkit_head_to_usd.py — typically pipeline/assets/arkit_head.usdc)

…and writes `<clip>_animated.usda` alongside the head asset. The output uses
the head USD as a *sublayer*, then authors per-frame `blendShapeWeights`
time samples on the existing `UsdSkelAnimation` prim. Sublayers compose at
the same path namespace, so we don't have to rewrite any internal prim
paths — and the user just opens the .usda in Maya / Blender / Houdini /
Omniverse / Unreal Sequencer and presses play.

Why pure-Python USD instead of Blender headless: `usd-core` is Pixar's
official pure-pip distribution (~30MB), so no DCC dependency lives in the
production image. The whole bake runs inside the existing
ProcessPoolExecutor with no extra setup.

Out of scope here:
  - Head pose (yaw/pitch/roll) animation. The CSV carries it but driving
    it through USD requires a real skeleton with a head joint. The existing
    apply scripts (apply_in_maya.py / apply_in_blender.py) consume those
    columns from the CSV directly.
  - Skinning weights — the rig is pure blendshapes.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from pxr import Sdf, Usd, UsdSkel, Vt

logger = logging.getLogger(__name__)


# CSV columns that aren't ARKit blendshape weights. Pose + identity cols.
_NON_BLENDSHAPE_COLS: frozenset[str] = frozenset({
    "frame", "time_seconds", "detected",
    "head_yaw", "head_pitch", "head_roll",
    "head_tx", "head_ty", "head_tz",
})


# The 52 standard ARKit blendshape names. Used to recover a clean blendshape
# identity from DCC-exporter-mangled USD names: mayaUSDExport names blendshapes
# after the *target geometry* with the deformer/namespace prefixed, e.g.
# `_Mesh_ncl1_1_jawOpen_jawOpenShape` rather than the deformer alias `jawOpen`.
# The CSV columns use clean ARKit names, so without this remap the bake matches
# zero columns and the animated USD comes out motionless.
_ARKIT_52: tuple[str, ...] = (
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft",
    "eyeLookUpLeft", "eyeSquintLeft", "eyeWideLeft",
    "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight",
    "eyeLookUpRight", "eyeSquintRight", "eyeWideRight",
    "jawForward", "jawLeft", "jawRight", "jawOpen",
    "mouthClose", "mouthFunnel", "mouthPucker", "mouthRight", "mouthLeft",
    "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
    "mouthDimpleLeft", "mouthDimpleRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper",
    "mouthPressLeft", "mouthPressRight",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "noseSneerLeft", "noseSneerRight",
    "tongueOut",
)
_ARKIT_BY_LOWER: dict[str, str] = {n.lower(): n for n in _ARKIT_52}


def _canonical_arkit_name(rig_name: str) -> str | None:
    """Recover the ARKit name a (possibly mangled) rig blendshape name encodes.

    Returns the canonical ARKit-52 name if `rig_name`, after dropping a trailing
    "Shape", ends with one (case-insensitive, longest match wins so
    `mouthStretchRight` isn't shadowed by `mouthRight`). The match must fall on a
    name boundary — start of string or a non-alphanumeric separator — so an
    embedded substring can't false-match. Returns None for names that encode no
    ARKit shape (custom rigs), leaving the caller's exact-match path in charge.
    """
    low = rig_name.lower()
    if low.endswith("shape"):
        low = low[: -len("shape")]
    best: str | None = None
    for arkit_lower, arkit in _ARKIT_BY_LOWER.items():
        if not low.endswith(arkit_lower):
            continue
        boundary = len(low) - len(arkit_lower)
        if boundary != 0 and low[boundary - 1].isalnum():
            continue  # suffix lands mid-token, not on a separator
        if best is None or len(arkit_lower) > len(best.lower()):
            best = arkit
    return best


@dataclass(frozen=True)
class BakeResult:
    """Summary of a bake — surfaced to the orchestrator for logging."""
    out_path: Path
    frame_count: int
    fps: float
    matched_columns: tuple[str, ...]      # CSV cols mapped to a rig target
    unmatched_columns: tuple[str, ...]    # CSV cols with no rig match (warned)
    rig_blendshape_count: int             # number of targets in the head USD


# ---------------------------------------------------------------------------
# Stage introspection — find the SkelAnimation prim and the blendshape order
# ---------------------------------------------------------------------------


def _find_skinned_mesh(stage: Usd.Stage) -> Usd.Prim:
    """First prim with UsdSkelBindingAPI applied and a non-empty blendShape list.

    mayaUSDExport names the mesh prim after the Maya mesh transform — usually
    `head_mesh` — but we don't depend on the name. We scan the stage.
    """
    for prim in stage.Traverse():
        if not prim.HasAPI(UsdSkel.BindingAPI):
            continue
        binding = UsdSkel.BindingAPI(prim)
        names_attr = binding.GetBlendShapesAttr()
        if names_attr and names_attr.Get():
            return prim
    raise RuntimeError(
        "No prim with UsdSkelBindingAPI + blendShapes was found in the head USD. "
        "Did you export with `mayaUSDExport -exportBlendShapes 1`?"
    )


def _find_or_create_skel_animation(
    stage: Usd.Stage,
    mesh: Usd.Prim,
) -> UsdSkel.Animation:
    """Return the SkelAnimation driving `mesh`, creating + binding one if absent.

    mayaUSDExport emits a SkelAnimation prim for any rig where blendshapes were
    exported (the deformer has weight curves to host). On the off chance the
    asset was authored without one (or a different exporter was used), we
    define one as a sibling of the mesh and wire `skel:animationSource`.
    """
    binding = UsdSkel.BindingAPI(mesh)
    sources = binding.GetAnimationSourceRel().GetTargets()
    if sources:
        prim = stage.GetPrimAtPath(sources[0])
        if prim and prim.IsA(UsdSkel.Animation):
            return UsdSkel.Animation(prim)

    # No source bound — create one alongside the mesh and wire it up.
    anim_path = mesh.GetPath().GetParentPath().AppendChild("Anim")
    if not stage.GetPrimAtPath(anim_path):
        anim = UsdSkel.Animation.Define(stage, anim_path)
    else:
        anim = UsdSkel.Animation(stage.GetPrimAtPath(anim_path))
    binding.CreateAnimationSourceRel().SetTargets([anim.GetPath()])
    return anim


# ---------------------------------------------------------------------------
# CSV → per-frame weight arrays
# ---------------------------------------------------------------------------


def _csv_blendshape_columns(header: list[str]) -> list[str]:
    return [c for c in header if c not in _NON_BLENDSHAPE_COLS]


def _build_weight_frames(
    rows: list[dict[str, str]],
    csv_cols: list[str],
    rig_blendshapes: list[str],
) -> tuple[list[list[float]], list[str], list[str]]:
    """Convert CSV rows to one [len(rig_blendshapes)]-float vector per frame.

    Match is case-insensitive. Returns (frames, matched_cols, unmatched_cols).
    Unmatched CSV columns are dropped (the rig has no slot for them); rig
    targets with no CSV column stay at 0.0.
    """
    # Map every key a CSV column might arrive under to its rig slot. The raw
    # lowercased rig name is primary (exact match, as before); the recovered
    # ARKit name is a fallback so mangled exporter names like
    # `_Mesh_ncl1_1_jawOpen_jawOpenShape` still resolve from the CSV's `jawOpen`.
    rig_index_by_lower: dict[str, int] = {}
    for i, name in enumerate(rig_blendshapes):
        rig_index_by_lower.setdefault(name.lower(), i)
        canon = _canonical_arkit_name(name)
        if canon is not None:
            rig_index_by_lower.setdefault(canon.lower(), i)

    col_to_rig: dict[str, int] = {}
    unmatched: list[str] = []
    for col in csv_cols:
        i = rig_index_by_lower.get(col.lower())
        if i is None:
            unmatched.append(col)
        else:
            col_to_rig[col] = i

    n_shapes = len(rig_blendshapes)
    frames: list[list[float]] = []
    for row in rows:
        w = [0.0] * n_shapes
        for col, idx in col_to_rig.items():
            try:
                w[idx] = float(row[col])
            except (KeyError, TypeError, ValueError):
                # Missing or malformed cell — leave as 0 and keep going.
                pass
        frames.append(w)

    return frames, sorted(col_to_rig.keys()), sorted(unmatched)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bake_animated_usd(
    csv_path: Path,
    head_usd_path: Path,
    out_path: Path,
    *,
    fps: float = 24.0,
) -> BakeResult:
    """Bake `csv_path` onto the SkelAnimation in `head_usd_path`, writing `out_path`.

    The output USDA references the head USDC as a *sibling sublayer* — the
    two files must end up in the same directory (the orchestrator places
    them in the bundle dir, so this holds for shipped bundles).

    `fps` controls timeCodesPerSecond + framesPerSecond on the output stage.
    Default is 24.0 to match capture.py's resampled output.
    """
    csv_path = Path(csv_path)
    head_usd_path = Path(head_usd_path)
    out_path = Path(out_path)

    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)
    if not head_usd_path.is_file():
        raise FileNotFoundError(head_usd_path)

    # --- 1. Discover the rig's blendshape order from the head USD. -------
    head_stage = Usd.Stage.Open(str(head_usd_path))
    if head_stage is None:
        raise RuntimeError(f"Could not open head USD: {head_usd_path}")
    mesh_prim = _find_skinned_mesh(head_stage)
    rig_blendshapes = list(
        UsdSkel.BindingAPI(mesh_prim).GetBlendShapesAttr().Get() or []
    )
    if not rig_blendshapes:
        raise RuntimeError(
            f"Mesh {mesh_prim.GetPath()} has an empty `skel:blendShapes` list."
        )
    logger.info(
        "[bake] head USD: %s — mesh %s with %d blendshapes",
        head_usd_path, mesh_prim.GetPath(), len(rig_blendshapes),
    )

    # --- 2. Read CSV. ----------------------------------------------------
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = list(reader.fieldnames or [])
    if not rows:
        raise RuntimeError(f"CSV has no data rows: {csv_path}")

    csv_cols = _csv_blendshape_columns(header)
    frames, matched, unmatched = _build_weight_frames(rows, csv_cols, rig_blendshapes)
    if unmatched:
        # Same UX as the apply scripts — warn loudly but don't fail.
        logger.warning(
            "[bake] CSV columns not present on the rig (skipped): %s",
            ", ".join(unmatched),
        )

    # --- 3. Build the output stage with the head USD as a sublayer. ------
    # Sublayers compose at the same paths, so we can directly author over
    # the SkelAnimation prim discovered in the head asset.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    out_stage = Usd.Stage.CreateNew(str(out_path))

    # Relative sublayer path — keeps the bundle portable.
    rel = os.path.relpath(head_usd_path, start=out_path.parent)
    out_stage.GetRootLayer().subLayerPaths.append(rel)

    # Match the head USD's defaultPrim so DCCs open the composed stage
    # cleanly. Falls back to leaving it unset (USD picks the first root prim).
    head_default = head_stage.GetDefaultPrim()
    if head_default:
        out_stage.SetDefaultPrim(out_stage.GetPrimAtPath(head_default.GetPath()))

    # --- 4. Author the animation. ----------------------------------------
    # Look up the mesh again on the composed stage so we resolve through
    # the sublayer; then bind / find the SkelAnimation.
    composed_mesh = out_stage.GetPrimAtPath(mesh_prim.GetPath())
    if not composed_mesh:
        raise RuntimeError(
            "Sublayer composition lost the mesh prim — check that "
            f"{head_usd_path.name} is reachable from {out_path.name}."
        )
    anim = _find_or_create_skel_animation(out_stage, composed_mesh)

    # `blendShapes` on the Anim is the authoritative weight order — mirror
    # the mesh's binding so anim[i] feeds blendShapeTargets[i].
    anim.CreateBlendShapesAttr(Vt.TokenArray(rig_blendshapes))

    weights_attr = anim.CreateBlendShapeWeightsAttr()
    for i, w in enumerate(frames):
        weights_attr.Set(Vt.FloatArray(w), Usd.TimeCode(float(i)))

    # --- 5. Stage timing. ------------------------------------------------
    n_frames = len(frames)
    out_stage.SetStartTimeCode(0.0)
    out_stage.SetEndTimeCode(float(max(0, n_frames - 1)))
    out_stage.SetTimeCodesPerSecond(fps)
    out_stage.SetFramesPerSecond(fps)
    # Don't override metersPerUnit / upAxis — they compose from the head USD.

    # --- 6. Save. --------------------------------------------------------
    out_stage.GetRootLayer().Save()
    logger.info(
        "[bake] wrote %s — %d frames at %g fps, %d matched / %d unmatched",
        out_path, n_frames, fps, len(matched), len(unmatched),
    )

    return BakeResult(
        out_path=out_path,
        frame_count=n_frames,
        fps=fps,
        matched_columns=tuple(matched),
        unmatched_columns=tuple(unmatched),
        rig_blendshape_count=len(rig_blendshapes),
    )

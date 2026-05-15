# Product Requirements Document: Facial Capture Pipeline

**Version:** 1.2
**Status:** Reflects the v2 simplification pass (2026-05-14). v2 ships as a public, no-auth, no-DB single-service web app with strict input constraints. v1.2 raises the hosted max clip duration from 5s to 7s.
**Last updated:** 2026-05-15
**Owner:** Shannon Gold

---

## 1. Summary

A facial animation capture pipeline that converts standard monocular video into ARKit-52 blendshape animation data, deliverable as a Maya- or Blender-ready package. Built for small VFX studios working with ARKit-standard rigs, with a focus on:

- Open-source / free tooling only
- CPU-only operation (no GPU dependency)
- Minimal operational footprint (single hosted service, no database)
- Predictable per-job cost
- A baseline that animators can polish from, not finished animation

The pipeline ships in two forms:

- **v1 local CLI** — the pipeline as a collection of Python scripts. Permissive about input format.
- **v2 hosted web app** — a single Railway service that serves a public dropzone page. No accounts, no database, strict input subset (1920×1080, ≤7s, .mp4). Distributed to a small known audience (~50 users) by URL.

---

## 2. Problem statement

### 2.1 The user's problem

Small VFX studios working on character animation receive footage of performers and need to convert that performance into facial animation on character rigs. The realistic alternatives today are:

- **Manual keyframing from reference:** time-consuming, requires skilled animators, doesn't scale
- **Commercial markerless tools (Faceware, Dynamixyz):** quality is good but licensing costs are prohibitive for small studios on a per-project basis
- **iPhone-based capture (Live Link Face):** requires reshooting with specific hardware, not always possible
- **Off-the-shelf ML solvers (MediaPipe, EMOCA):** technically free but require enough technical infrastructure that they're rarely used in practice by small studios

Small studios need a middle ground: free or cheap, no special capture hardware, no GPU infrastructure, and "good enough as a baseline that animators polish from" quality.

### 2.2 What this tool addresses

Convert existing monocular video footage into:

1. A blendshape animation CSV using ARKit's standard 52-target naming
2. A preview video showing tracking quality
3. Maya and Blender scripts that apply the animation to a user's rig

Without requiring:
- GPU compute
- Paid software licenses
- Special capture hardware
- Per-actor model training
- ML/CV expertise from the end user

### 2.3 Non-goals

Explicitly out of scope:

- Real-time / streaming capture
- Multi-face capture (deferred to v2+)
- Capture from non-video sources (depth, MoCap, audio-driven)
- Per-actor calibration (deferred to v2+)
- Body / hand capture
- Rendering finished animation (only data + thin preview)
- Cinematic-grade or "final-pixel" quality
- Custom blendshape rigs in v1 (ARKit-52 only initially)
- **(v2)** User accounts, login, job history, email notifications, rate limiting, quotas, audit trail. The hosted product is a one-shot tool, not a service with users.

---

## 3. Users & use cases

### 3.1 Primary user

Small VFX/animation studio with:
- 1–10 animators
- Ad-hoc facial animation needs
- ARKit-standard rigs (often from ReadyPlayerMe, MetaHuman, or in-house with ARKit naming)
- Existing Maya / Blender pipeline
- No budget for Faceware-tier tooling
- No internal ML / CV expertise

### 3.2 Secondary user

Solo character artists or freelancers working in the same space.

### 3.3 Concrete use cases

**UC-1: Reference baseline from existing footage**
Studio has footage of a performer (actor, voice-over recording with camera, etc.). They want a blendshape animation pass on the matching scene that an animator will then polish.

**UC-2: Iteration during animation review**
Animator wants to quickly turn a director's reference performance into a starting point for a shot, rather than keyframing from scratch.

**UC-3: Lipsync baseline**
Studio has dialogue recording with video and needs lipsync as a starting point for an animation pass.

### 3.4 Anti-personas

This product is explicitly *not* for:

- AAA game studios or feature film houses (need Faceware-tier quality, can afford it)
- VTubers / live streamers (need real-time, different pipeline entirely)
- Indie game devs targeting MetaHuman (Epic's MetaHuman Animator is purpose-built and free)
- Hobbyists with no Maya / DCC pipeline (no entry point into the tool)

---

## 4. Functional requirements

### 4.1 Local pipeline (v1, current state)

#### FR-1: Source video ingestion
- Accept MP4, MOV, AVI, MKV input
- Source resolution: nominally 1080p; pipeline must handle 720p–4K gracefully
- Source frame rate: nominally 24fps; pipeline must handle 23.976–60fps
- Source duration: optimized for ≤5s, must handle up to 60s without architectural changes
- Single performer in frame (v1 constraint)

#### FR-2: Face-aware preprocessing
- Detect the face position across multiple sampled frames
- Compute a stable square crop region with configurable margin (default 30%)
- Upscale to a target resolution (default 1080×1080)
- Output a preprocessed MP4 ready for capture

#### FR-3: Temporal upsampling
- Optional: interpolate source to 60fps internally using ffmpeg `minterpolate` (CPU-only)
- Improves tracking quality on slow/sustained motion
- User-selectable on/off (default: on for production, off for iteration)
- Note: minterpolate is the floor; higher-quality interpolation (RIFE) supported but requires user-provided binary

#### FR-4: Blendshape capture
- Run MediaPipe Face Landmarker in VIDEO mode
- Output 52 ARKit-standard blendshape weights per frame
- Output 6-DOF head pose (yaw/pitch/roll + translation) per frame
- Output detection confidence per frame (binary detected/not)
- CSV format: `frame, time_seconds, <52 ARKit names>, head_yaw, head_pitch, head_roll, head_tx, head_ty, head_tz, detected`

#### FR-5: Temporal smoothing
- Apply One Euro filter per channel
- Filter parameters auto-tuned to capture frame rate
- Detection-gap-aware: reset filter state when face was not detected
- Pose columns smoothed with gentler parameters than blendshape columns
- Output preserves CSV format

#### FR-6: Frame rate resampling
- Linearly interpolate CSV from capture frame rate to delivery frame rate (default: capture at 60fps internally, deliver at source 24fps)
- Preserve `detected` column as nearest-neighbor (not linearly interpolated)

#### FR-7: Preview rendering
- Render mesh overlay onto the original source video (at source frame rate)
- Overlay: face mesh tesselation, contours, lips, eyes, irises
- Corner overlay: bar graph of top-N most active blendshapes (from CSV values, not re-solved)
- Status line: frame number, processing rate, face detection status
- Output: MP4 matching source resolution and frame rate

#### FR-8: Maya & Blender application script generation
- Per-job generated Python scripts: `apply_in_maya.py` and
  `apply_in_blender.py` (user picks whichever matches their DCC)
- CSV path pre-baked (relative to script location by default)
- Zero-config by default — no names to look up or edit:
  - Maya: auto-detects every blendShape node (`cmds.ls(type='blendShape')`)
  - Blender: auto-detects every mesh with shape keys
- Optional override in `USER CONFIG` to restrict application to specific
  nodes/objects in unusual scenes (`BLENDSHAPE_NODES` / `MESH_OBJECTS`)
- Auto-detect target aliases (Maya) / shape-key names (Blender)
- Handle split ARKit rigs (head + eyes + teeth) across multiple nodes/meshes
- Skip unmatched columns gracefully; warn loudly if nothing matches anywhere
  (signals a non-ARKit rig)
- Clear existing animation on matched attributes/shape keys before re-keying
  (configurable)
- Both scripts emit identical `[apply]` progress lines and use linear
  interpolation for parity

#### FR-9: User-facing documentation
- README.txt generated per job with:
  - Bundle contents description
  - Three-step usage instructions
  - Common gotchas and how to handle them

#### FR-10: Bundle assembly
- Final deliverable: directory containing the five files (preview.mp4, blendshapes.csv, apply_in_maya.py, apply_in_blender.py, README.txt)
- Optional zip packaging

### 4.2 Hosted service (v2, simplified)

The hosted product is a thin web wrapper around the v1 pipeline. It exists to give a small audience (~50 known users) a single URL they can drop a video into, with no install, no login, and no key entry. Anything beyond that minimum is out of scope.

#### FR-11: Public single-page web app
- One URL serves a polished landing page with a drag-and-drop dropzone.
- No authentication. No accounts. No login. No API key. URL is distributed privately to the trusted audience.
- The page validates input on the client and rejects anything that fails before bytes hit the network.

#### FR-12: Strict input constraints (hard limits)

The hosted product accepts a deliberately narrow input subset:

| Constraint | Value | Enforcement |
|---|---|---|
| Resolution | exactly **1920 × 1080** | Client probes video metadata; rejects others. |
| Duration | **≤ 7 seconds** | Client probes duration; rejects longer clips. |
| Container | **.mp4** only | Client checks `File.type` / extension. |
| Max file size | 500 MB | Client check + backend `MAX_UPLOAD_BYTES`. |

The local CLI (FR-1) remains permissive; the hosted product is the strict subset.

#### FR-13: Async pipeline execution with status polling
- Upload returns immediately with a job id and `status=queued`.
- The pipeline runs in-process on the same Railway service (single `ProcessPoolExecutor` worker per instance) and updates an in-memory job record.
- The page polls `GET /api/jobs/{id}` every 2s until the status is terminal.
- On `succeeded`, the page surfaces a download button; on `failed`, it shows the error.

#### FR-14: Signed bundle download
- Successful jobs upload `bundles/{job_id}.zip` to R2 with a 1-hour signed URL.
- The browser is redirected to the signed URL directly; the app does not proxy the download.
- Bundle expiry is enforced by R2 lifecycle rules on the bucket (see FR-15), not by application code.

#### FR-15: Bundle TTL via R2 lifecycle
- The R2 bucket is configured with a lifecycle rule that auto-deletes objects after a fixed retention period (default 7 days).
- No application-side cleanup task; no scheduled job; no database column tracking expiry.
- The lifecycle rule is configured once in the Cloudflare dashboard (or via the R2 API) and is part of the deploy checklist, not the running code.

#### FR-16: Health endpoint
- `GET /health` returns `200 {"status": "ok"}`. Public, used by Railway's healthcheck.

### 4.3 Removed from v2 (explicit non-requirements)

The following appeared in earlier drafts and are intentionally **not** part of the hosted product:

- ~~Studio account management (was FR-14)~~
- ~~Email notifications~~
- ~~Database / persistent job history~~
- ~~Per-account quotas / rate limiting~~
- ~~Audit trail / logs UI~~
- ~~Job re-download after page reload~~ (state is in-memory; refresh loses the active job id unless the SPA stashes it locally)
- ~~Shared `X-API-Key` auth~~

If any of these become needed, they constitute a v3 scope expansion and would re-introduce a database. Decision deferred until real demand exists.

---

## 5. Non-functional requirements

### 5.1 Performance

| Operation | Target | Hard limit |
|---|---|---|
| Local: full pipeline w/ interpolation, 5s clip | <5 min | <10 min |
| Local: full pipeline w/o interpolation, 5s clip | <30 sec | <60 sec |
| Hosted: end-to-end including upload/download, 7s clip | <7 min | <15 min |

The local rows benchmark a 5s clip (the v1 CLI's standard regression clip, unchanged). The hosted row now benchmarks the new 7s ceiling. **The hosted target/limit numbers above were calibrated at 5s; a 7s clip is ~40% more frames and proportionally more compute. Re-measure end-to-end at 7s and revise these figures before treating them as committed targets.**

Queue-time targets are no longer meaningful — with a single in-process worker and 50 occasional users, concurrent jobs are rare. The page surfaces queued state honestly when it happens.

### 5.2 Quality

**Quality bar (v1):** "Acceptable baseline for animator polish" — the captured animation is recognizable as the performer's, key expressions land, gross timing is correct. The animator should spend less time than keyframing from scratch.

**Quality bar (v1) is not:** "Client-ready final pixel" — visible solver artifacts (forehead/brow drift, muted small expressions) are expected. The polish step is the animator's responsibility.

**Concretely measurable:**
- Face detection rate across a 7s clip: ≥98%
- Major blendshape responsiveness: jawOpen, mouthSmileLeft/Right, eyeBlinkLeft/Right should each reach >0.7 when the performer clearly executes that expression
- Lipsync timing accuracy: blendshape onsets should land within 2 frames (at 24fps) of audio events

### 5.3 Reliability

- **Local pipeline:** any individual step failure must surface a clear error message naming the failed step; no silent partial output.
- **Hosted service:** target 99% successful job completion rate (excluding user-error inputs).
- **No persistence guarantee:** a service restart drops in-memory job state. A user mid-poll sees `404 — job no longer exists` and re-uploads. This is acceptable because the audience is small and the operation is one-shot.

### 5.4 Cost (hosted v2)

- **Per-job processing cost:** target <$0.10/job at moderate scale (CPU compute on Railway, R2 storage).
- **Storage cost:** R2 lifecycle deletes bundles after retention; no orphan accumulation.
- **Monthly operational ceiling:** target <$15/month at the expected ~50 users × a handful of jobs each. Postgres removal eliminates the Railway DB add-on cost.

### 5.5 Operational footprint

- **One Railway service** running the Dockerfile in this repo (FastAPI + the pipeline).
- **One R2 bucket** for videos and bundles, with a lifecycle rule for TTL.
- **No database.** Job state is an in-memory `dict` in the Python process.
- **No external paid APIs in critical path.** No Replicate, no OpenAI, no Resend, no per-call services.
- **No GPU instances.**

### 5.6 Compliance / IP

- All pipeline software uses permissive open-source licenses (MediaPipe = Apache 2.0; ffmpeg = LGPL/GPL with appropriate care).
- User-uploaded video content treated as confidential; not used for training, not shared.
- ARKit blendshape *names* are an Apple-published standard; usage of the naming convention is not a licensing concern.
- Bundled reference rigs (if added in future) must come with clear commercial-use licensing.

---

## 6. System architecture

### 6.1 Local pipeline architecture

```
┌─────────────┐
│ source.mp4  │  user-supplied input (24fps, 1080p, ≤5s)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  crop.py    │  detect face, compute stable square crop, upscale
└──────┬──────┘
       │
       ▼ (optional, default on)
┌──────────────────┐
│ ffmpeg minterpol │  24 → 60fps temporal interpolation
└──────┬───────────┘
       │
       ▼
┌──────────────┐
│  capture.py  │  MediaPipe Face Landmarker → 52 ARKit weights/frame
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  smooth.py   │  One Euro filter (cutoff auto-tuned to capture fps)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ resample.py  │  60 → 24fps linear interpolation per channel
└──────┬───────┘
       │
       ▼
┌────────────────────┐
│ preview_overlay.py │  mesh overlay on original source
└──────┬─────────────┘
       │
       ▼
┌──────────────────┐
│  orchestrator.py │  assembles bundle:
│  bundle assembly │  • preview.mp4
│                  │  • blendshapes.csv (24fps)
│                  │  • apply_in_maya.py (CSV path baked)
│                  │  • apply_in_blender.py (CSV path baked)
│                  │  • README.txt
└──────────────────┘
```

### 6.2 Hosted service architecture (v2)

```
┌─────────────────────────────────────────┐
│  Browser (the user's 50-friend audience)│
│                                         │
│  Drops a 1920×1080 ≤7s .mp4 onto        │
│  the dropzone. Client validates and     │
│  rejects locally if invalid.            │
└────────────────┬────────────────────────┘
                 │ POST /api/jobs (multipart, no auth header)
                 ▼
┌─────────────────────────────────────────┐
│  Railway service (single container)     │
│  ┌─────────────────────────────────┐    │
│  │ FastAPI                         │    │
│  │   GET  /health                  │    │
│  │   POST /api/jobs                │    │
│  │   GET  /api/jobs/{id}           │    │
│  │   GET  /api/jobs/{id}/bundle    │    │
│  │   GET  /                        │    │
│  │   GET  /assets/*                │    │
│  └────────────┬────────────────────┘    │
│               │ asyncio.create_task     │
│               ▼                         │
│  ┌─────────────────────────────────┐    │
│  │ In-process pipeline runner      │    │
│  │  (ProcessPoolExecutor, n=1)     │    │
│  │  state: dict[uuid, JobRecord]   │    │
│  └────────────┬────────────────────┘    │
└───────────────┼─────────────────────────┘
                │
                │ put/get/delete
                ▼
        ┌───────────────────┐
        │ Cloudflare R2     │  sources + bundles
        │  + lifecycle rule │  (auto-deletes after 7 days)
        └───────────────────┘
```

No Postgres. No worker queue. No auth layer. No cleanup task. The service starts uvicorn, mounts the SPA, and handles uploads inline.

### 6.3 Component inventory

| Component | Layer | Tech | Status |
|---|---|---|---|
| `crop.py` | Pipeline step | Python + MediaPipe + ffmpeg | Complete |
| `capture.py` | Pipeline step | Python + MediaPipe | Complete |
| `smooth.py` | Pipeline step | Python (pure) | Complete |
| `resample.py` | Pipeline step | Python (pure) | Complete |
| `preview_overlay.py` | Pipeline step | Python + OpenCV + MediaPipe | Complete |
| `orchestrator.py` | Pipeline driver | Python | Complete |
| `apply_in_maya.py` | User artifact | Python (Maya) | Generated per job |
| `apply_in_blender.py` | User artifact | Python (Blender `bpy`) | Generated per job |
| `interp.py` | Optional step | Python + RIFE/ffmpeg | Complete (RIFE optional) |
| `retarget.py` | Auxiliary CLI | Python | Complete (not in v1/v2 path) |
| `app/main.py` + `app/jobs.py` | Hosted web layer | Python + FastAPI | Complete (v2) |
| `app/storage.py` | Hosted storage layer | boto3 / R2 + LocalStorage dev fallback | Complete (v2) |
| SPA (`web/`) | Client | React + Vite + TypeScript | Complete (interim); polish pass pending design |
| Dockerfile + railway.toml | Deploy | Multi-stage Docker | Complete (v2) |

### 6.4 Data model

**There is no persistent data model.**

Job state lives in a module-level `dict[uuid.UUID, JobRecord]` in `app/jobs.py`. A `JobRecord` is an in-memory dataclass with the same fields the API surfaces:

```
id, status, created_at, started_at, completed_at,
error_log, bundle_key, source_duration_seconds
```

A process restart loses all in-flight and terminal records. R2 still holds any bundle that was uploaded before the restart, but the application no longer has a job id pointing at it; those bundles age out via the R2 lifecycle rule. This is a deliberate trade-off — see §5.3.

**Object storage layout (R2):**

```
sources/{job_id}.<ext>       source video upload (deleted on success)
bundles/{job_id}.zip         delivery bundle (auto-deleted by lifecycle rule)
```

---

## 7. User flows

### 7.1 Local pipeline (v1)

**Setup (one time):**
```bash
git clone <repo>
cd face-capture
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg
wget <face_landmarker.task URL>
```

**Per job:**
```bash
python -m pipeline.orchestrator source.mp4 -o ./jobs/myjob --zip
```

**Apply in Maya or Blender:**
1. Unzip bundle if zipped
2. Open the scene with the ARKit-rigged head
3. Run the script for your DCC:
   - Maya: `apply_in_maya.py` in the Script Editor (Python tab)
   - Blender: `apply_in_blender.py` in the Scripting workspace

Both scripts auto-detect the rig and the baked CSV path — no lookup or
editing required. (Optional override: `BLENDSHAPE_NODES` / `MESH_OBJECTS`
to restrict to specific nodes/objects in an unusual scene.)

### 7.2 Hosted service (v2)

**Submit a job:**
1. User loads the public URL.
2. Drag-and-drops a `.mp4` onto the dropzone (or clicks to pick a file).
3. The page probes the video's resolution and duration. If anything fails the 1920×1080 / ≤7s / .mp4 check, the page shows a clear error and never starts the upload.
4. If valid, the file is `POST`ed to `/api/jobs` (multipart). The server streams to R2, creates an in-memory job record, and returns `{ id, status: "queued" }`.
5. The page transitions to a status view and polls `/api/jobs/{id}` every 2s.

**Job lifecycle:**
1. Backend spawns `asyncio.create_task(_run_pipeline_for_job(...))` immediately on upload.
2. Pipeline runner downloads the source from R2 to a tempdir, runs `pipeline.orchestrator.run_pipeline` in a `ProcessPoolExecutor` (so MediaPipe doesn't block the event loop).
3. Bundle uploaded to R2 at `bundles/{id}.zip`. In-memory record's `status` flips to `succeeded` with `bundle_key` set.
4. On failure, traceback is captured into `error_log` and `status` flips to `failed`.

**Download bundle:**
1. Page sees terminal status and shows a "Download bundle" button.
2. Button calls `/api/jobs/{id}/bundle` which returns a 1-hour signed R2 URL.
3. Browser is redirected to the signed URL; Cloudflare serves the zip directly.

---

## 8. Quality strategy

### 8.1 Acceptance criteria for v1

A v1 release is acceptable when:

- [ ] `python -m pipeline.orchestrator test.mp4 -o ./jobs/smoketest --no-interpolate` completes in <60s on a modest laptop
- [ ] Same with interpolation completes in <10 min on the same hardware
- [ ] Output bundle contains exactly the five expected files
- [ ] CSV opens cleanly in a spreadsheet; all 52 ARKit columns present; no NaN values
- [ ] Preview MP4 plays cleanly; mesh overlay tracks face on most frames
- [ ] Apply script runs without errors on a standard ARKit-named rig — Maya (`apply_in_maya.py`) and Blender (`apply_in_blender.py`)
- [ ] Animator review (with at least one real animator) confirms output is "usable as a baseline"

### 8.2 Acceptance criteria for v2 (hosted)

- [ ] All v1 criteria still pass through the hosted path
- [ ] Job success rate ≥99% over 50+ test jobs (where input matches the strict constraints)
- [ ] No data leaks between jobs (signed URLs scoped per-object, bucket private, no listing)
- [ ] R2 lifecycle rule verified to delete bundles older than the retention period
- [ ] Operational cost matches projections (FR 5.4)
- [ ] A user with the URL and a valid clip can produce a bundle with **zero** prior interaction with the operator

### 8.3 Test plan

**Local pipeline regression:**
- Curate a small set of test videos (5–10 clips):
  - Various performers (range of skin tones, beards/clean-shaven, with/without glasses)
  - Various lighting (flat, dramatic, low-light)
  - Various performances (talking head, expressive, subtle)
  - 24fps and 30fps source
- Run the full pipeline on each
- Snapshot key metrics: detection rate, blendshape value ranges, processing time
- Compare against snapshot on each release

**Hosted regression:**
- `scripts/smoke_test.sh` exercises the full upload → poll → download path against the deployed URL.
- The same test videos are reused; the smoke test asserts on bundle contents and CSV sanity.
- No DB / auth assertions in the smoke test (deliberately removed in v1.1).

### 8.4 Known limitations (document, do not fix)

These are inherent to the MediaPipe solver and cannot be addressed at the pipeline level:

1. **Forehead / eyebrow drift** — generic prior, no per-actor calibration
2. **Subtle expression muting** — 52-bucket classification doesn't capture fine muscle motion
3. **Fast lip-sync limitations at 24fps** — undersampling, interpolation helps marginally
4. **Lazy blinks at 24fps** — same undersampling reason
5. **Tongue tracking unreliable** — `tongueOut` blendshape rarely fires correctly
6. **Asymmetric expressions partially flattened** — solver tends toward symmetric solutions

These should be visible in `README.txt` as "Known limitations of this pipeline tier" so users have correct expectations.

---

## 9. Future work / roadmap

### 9.1 v1.x (post-launch local improvements)

- **Channel-aware smoothing:** different filter parameters for brow vs. lip vs. eye channels
- **Per-target scaling in apply script:** user-configurable scale per blendshape (e.g., boost eye blinks 1.8×)
- **Noise floor threshold:** clip channel values below threshold to zero (kills idle jitter)
- **Frame-rate-aware filter retuning:** more sophisticated than current 2-tier
- **Optional per-actor calibration:** user supplies a neutral frame + ROM clip, system computes per-channel scaling factors

### 9.2 v2.x (hosted polish, if demand justifies)

- **Wider input acceptance:** relax the strict 1920×1080 / ≤7s constraints once the pipeline proves robust to variation in the hosted environment.
- **In-browser preview viewer:** play the resulting preview.mp4 inline before download.
- **Drag-multiple, queue-locally:** still single job at a time on the server, but the page lets the user line up several clips and submit them sequentially.
- **Client-side resolution downscale:** for users who shoot 4K, offer browser-side ffmpeg.wasm transcoding rather than rejection.

### 9.3 v3 (re-introduces persistence if any of these become required)

Any of the following individually re-justifies a database and an auth layer:

- Per-user accounts / job history
- Email notifications on completion
- Audit trail / usage analytics
- Rate limiting beyond what Railway's edge already provides
- Public release beyond the trusted ~50-user audience
- **Quality escalation tier with RIFE / EMOCA / GPU compute** (also requires billing)

Each of these is a meaningful scope expansion and should be planned as a v3 release with its own PRD pass, not added piecemeal to v2.

### 9.4 Explicitly not on roadmap

- Multi-camera / stereo capture
- Live / streaming capture
- Body or hand capture
- Audio-driven animation
- Real-time API for game engine integration
- Mobile app

These would be different products built on different premises.

---

## 10. Risks & mitigations

### 10.1 Technical risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MediaPipe quality is insufficient for any real use case | Medium | High | Validate with one real animator on real clips before further investment |
| ffmpeg minterpolate is too slow for hosted CPU constraints | Low → Medium | Medium | ~3 min for a 5s clip (measured); a 7s clip scales to ~4+ min, eating more of the <7 min hosted target. Re-measure at 7s — see §5.1 calibration note. |
| Maya rig variation breaks apply script | Medium | Medium | Apply script handles multi-node ARKit splits; document common rig types |
| MediaPipe API changes in future versions | Low | Low | Pin version range in requirements.txt |
| Railway service restart kills in-flight job | Medium | Low | Acknowledged in §5.3; user re-uploads. R2 bundles aren't lost; only the dict entry is. |
| Railway service interruption | Low | Medium | Local pipeline is the fallback |

### 10.2 Product risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Users expect Faceware-quality output | High | High | Landing copy frames product as "baseline for animator polish" |
| Trusted-URL audience grows past intent | Low | Medium | Re-evaluate auth model if the URL spreads; §9.3 lays out the path |
| ARKit-only is too restrictive | Medium | Medium | Custom rig mapping exists in `retarget.py`; can be surfaced if demand emerges |

### 10.3 Operational risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| User uploads inappropriate content | Medium | High | Audience is known and trusted; reactive policy. Re-evaluate before any public release. |
| Storage costs balloon | Low | Low | R2 lifecycle deletes bundles automatically; sources deleted on success. |
| Spam / abuse via the public URL | Low | Medium | URL is shared privately; can rotate the deploy URL if it leaks. Backend size + duration limits cap any individual attack. |

---

## 11. Open questions

The pre-pivot draft listed five open questions; the simplification pass closed all of them. Recorded here for posterity:

1. ~~Authentication for hosted v2~~ → **Closed.** No auth.
2. ~~Pricing model~~ → **Closed.** Free for the trusted audience. Pricing belongs to a v3 scope.
3. ~~Frontend stack~~ → **Closed.** React + Vite SPA in `web/`.
4. ~~Hosting provider~~ → **Closed.** Railway.
5. ~~Email notifications~~ → **Closed.** No email. Status polling only.

Genuinely open questions for v2.x:

- Should the deploy URL be vanity-named (e.g., a custom domain) or stay on the auto-generated `*.up.railway.app`?
- Is there a real need for the SPA to remember the in-flight job id across reloads (via `localStorage`)? Currently it does not, because the server-side dict is the source of truth.

---

## 12. Glossary

- **ARKit-52:** Apple's standard set of 52 facial blendshape names used in iOS face tracking and widely adopted across the industry.
- **Blendshape:** A named morph target on a mesh that, when weighted from 0 to 1, deforms the mesh toward a specific facial expression (e.g., "mouthSmileLeft").
- **MediaPipe:** Google's open-source ML framework. The Face Landmarker model emits ARKit-52 weights and 478-point facial landmarks.
- **MediaPipe Face Mesh:** The 478-point landmark topology MediaPipe places on the face.
- **One Euro filter:** Adaptive low-pass filter (Casiez et al, 2012) that smooths slow motion aggressively while preserving fast motion.
- **FLAME:** A research-grade anatomical face model used by higher-quality solvers like EMOCA.
- **Markerless capture:** Facial capture using only the video image, no physical markers.
- **Retargeting:** Mapping animation data from one rig/format to another.
- **CRF / minterpolate / mci / aobmc:** ffmpeg-specific parameters.
- **WSL2:** Windows Subsystem for Linux v2.
- **R2:** Cloudflare's S3-compatible object storage. Cheaper than S3 for egress.
- **Railway:** A Heroku-like platform-as-a-service.

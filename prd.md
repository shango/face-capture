# Product Requirements Document: Facial Capture Pipeline

**Version:** 1.0
**Status:** Draft
**Last updated:** 2026-05-13
**Owner:** Shannon Gold

---

## 1. Summary

A facial animation capture pipeline that converts standard monocular video into ARKit-52 blendshape animation data, deliverable as a Maya-ready package. Built for small VFX studios working with ARKit-standard rigs, with a focus on:

- Open-source / free tooling only
- CPU-only operation (no GPU dependency)
- Simple operational footprint (small studio infrastructure)
- Predictable per-job cost
- A baseline that animators can polish from, not finished animation

The pipeline starts as a local CLI tool (current state), then progresses to a small hosted web service on Railway.

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

Convert existing monocular video footage (24fps, 1080p, single subject, ≤5 second clips) into:

1. A blendshape animation CSV using ARKit's standard 52-target naming
2. A preview video showing tracking quality
3. A Maya script that applies the animation to a user's rig

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

#### FR-8: Maya application script generation
- Per-job generated Python script
- CSV path pre-baked (relative to script location by default)
- Clear `USER CONFIG` section for blendShape node names and fps
- Auto-detect target aliases on each blendShape node
- Handle multiple blendShape nodes (for split ARKit rigs: head + eyes + teeth)
- Skip unmatched columns gracefully with warnings
- Clear existing animation on matched attributes before re-keying (configurable)

#### FR-9: User-facing documentation
- README.txt generated per job with:
  - Bundle contents description
  - Three-step usage instructions
  - Common gotchas and how to handle them

#### FR-10: Bundle assembly
- Final deliverable: directory containing the four files (preview.mp4, blendshapes.csv, apply_in_maya.py, README.txt)
- Optional zip packaging

### 4.2 Hosted service (v2, planned)

#### FR-11: Web upload
- User uploads source video via web form
- Form accepts file directly or signed upload URL flow for larger files
- Max file size: 100 MB (covers 5s 1080p at high bitrate plus headroom)
- Supported types as per FR-1

#### FR-12: Async job processing
- Each upload creates a job record in the database
- Pipeline runs in a background worker process
- User polls for completion or receives email notification
- Target latency: ≤7 minutes per 5-second clip including interpolation

#### FR-13: Job status / management
- Each job has a unique ID and a status: queued, running, succeeded, failed
- Logs from each pipeline step retained for failed jobs (debugging support)
- Successful jobs produce a download bundle (zip) with signed URL
- Job artifacts retained for configurable period (default: 7 days)

#### FR-14: Studio account management (v2.1)
- Per-studio accounts with login
- Per-account job history
- Per-account quota (initially generous, may be tightened based on usage patterns)

---

## 5. Non-functional requirements

### 5.1 Performance

| Operation | Target | Hard limit |
|---|---|---|
| Local: full pipeline w/ interpolation, 5s clip | <5 min | <10 min |
| Local: full pipeline w/o interpolation, 5s clip | <30 sec | <60 sec |
| Hosted: end-to-end including upload/download, 5s clip | <7 min | <15 min |
| Hosted: queue-time-to-start under normal load | <30 sec | <2 min |

### 5.2 Quality

**Quality bar (v1):** "Acceptable baseline for animator polish" — the captured animation is recognizable as the performer's, key expressions land, gross timing is correct. The animator should spend less time than keyframing from scratch.

**Quality bar (v1) is not:** "Client-ready final pixel" — visible solver artifacts (forehead/brow drift, muted small expressions) are expected. The polish step is the animator's responsibility.

**Concretely measurable:**
- Face detection rate across 5s clip: ≥98%
- Major blendshape responsiveness: jawOpen, mouthSmileLeft/Right, eyeBlinkLeft/Right should each reach >0.7 when the performer clearly executes that expression
- Lipsync timing accuracy: blendshape onsets should land within 2 frames (at 24fps) of audio events

### 5.3 Reliability

- **Local pipeline:** any individual step failure must surface a clear error message naming the failed step; no silent partial output
- **Hosted service:** target 99% successful job completion rate (excluding user-error inputs like unparseable videos)
- **No data loss:** uploaded videos retained until job artifacts are delivered; failed jobs retain logs for inspection

### 5.4 Cost (hosted v2)

- **Per-job processing cost:** target <$0.10/job at moderate scale (CPU compute on Railway, R2 storage)
- **Storage cost:** signed URLs auto-expire bundles; cleanup job purges artifacts after retention period
- **Per-studio monthly cost ceiling:** target <$50/month operational cost for 100 jobs

### 5.5 Operational footprint

- One Railway service (Python worker + thin HTTP wrapper)
- One Postgres database for job state
- One R2/S3 bucket for video and bundle storage
- No external paid APIs in critical path (no Replicate, no OpenAI, no per-call services)
- No GPU instances required

### 5.6 Compliance / IP

- All pipeline software uses permissive open-source licenses (MediaPipe = Apache 2.0; ffmpeg = LGPL/GPL with appropriate care)
- User-uploaded video content treated as confidential; not used for training, not shared
- ARKit blendshape *names* are an Apple-published standard; usage of the naming convention is not a licensing concern
- Bundled reference rigs (if added in future) must come with clear commercial-use licensing

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
│  pipeline.py     │  assembles bundle:
│  bundle assembly │  • preview.mp4
│                  │  • blendshapes.csv (24fps)
│                  │  • apply_in_maya.py (CSV path baked)
│                  │  • README.txt
└──────────────────┘
```

### 6.2 Hosted service architecture (v2)

```
┌─────────────────┐
│  Web frontend   │  Cloudflare Pages or similar
└────────┬────────┘
         │ POST /jobs (with video upload)
         ▼
┌─────────────────────────────────┐
│  FastAPI service on Railway     │
│  ┌──────────────────────────┐   │
│  │ HTTP API                 │   │
│  │  POST /jobs              │   │
│  │  GET  /jobs/{id}         │   │
│  │  GET  /jobs/{id}/bundle  │   │
│  └──────────┬───────────────┘   │
│             │                   │
│  ┌──────────▼───────────────┐   │
│  │ Job queue worker          │   │
│  │  (calls pipeline.py)      │   │
│  └──────────┬───────────────┘   │
└─────────────┼───────────────────┘
              │                    ┌─────────────────┐
              ├─────read/write────►│ Railway Postgres │  (job state)
              │                    └─────────────────┘
              │                    ┌─────────────────┐
              └────read/write─────►│ Cloudflare R2    │  (videos + bundles)
                                   └─────────────────┘
```

### 6.3 Component inventory

| Component | Layer | Tech | Status |
|---|---|---|---|
| `crop.py` | Pipeline step | Python + MediaPipe + ffmpeg | Complete |
| `capture.py` | Pipeline step | Python + MediaPipe | Complete |
| `smooth.py` | Pipeline step | Python (pure) | Complete |
| `resample.py` | Pipeline step | Python (pure) | Complete |
| `preview_overlay.py` | Pipeline step | Python + OpenCV + MediaPipe | Complete |
| `pipeline.py` | Orchestrator | Python | Complete |
| `apply_in_maya.py` | User artifact | Python (Maya) | Generated per job |
| `interp.py` | Optional step | Python + RIFE/ffmpeg | Complete (RIFE optional) |
| `retarget.py` | Auxiliary | Python | Complete (not in v1 path) |
| FastAPI wrapper | Hosted layer | Python + FastAPI | Planned (v2) |
| Job queue | Hosted layer | Postgres LISTEN/NOTIFY or arq | Planned (v2) |
| R2 integration | Storage layer | boto3 with R2 endpoint | Planned (v2) |
| Frontend | Client | TBD (HTML form sufficient for v2.0) | Planned (v2) |

### 6.4 Data model (v2)

**`jobs` table (Postgres):**

| Column | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| studio_id | uuid | FK to studios (when accounts added) |
| status | enum | queued, running, succeeded, failed |
| source_video_url | text | R2 signed URL or key |
| bundle_url | text | R2 signed URL when complete |
| created_at | timestamptz | |
| started_at | timestamptz | nullable |
| completed_at | timestamptz | nullable |
| error_log | text | nullable, populated on failure |
| pipeline_config | jsonb | which flags were used (interpolate on/off, etc.) |
| source_duration_seconds | numeric | for billing/quota |
| expires_at | timestamptz | auto-cleanup deadline |

**Object storage layout (R2):**

```
videos/{job_id}.mp4         source video upload
bundles/{job_id}.zip        delivery bundle
intermediates/{job_id}/     work files (auto-cleaned)
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
python pipeline.py source.mp4 -o ./jobs/myjob --zip
```

**Apply in Maya:**
1. Unzip bundle if zipped
2. Open Maya scene with ARKit-rigged head
3. Find blendShape node names via `cmds.ls(type='blendShape')`
4. Edit `BLENDSHAPE_NODES = [...]` in `apply_in_maya.py`
5. Run the script in Maya's Script Editor

### 7.2 Hosted service (v2)

**Submit a job:**
1. User loads the web upload page
2. Drops a video file into the form
3. Form POSTs to `/jobs` endpoint, which:
   - Uploads video to R2
   - Creates a `jobs` row with `status=queued`
   - Returns `{ job_id }`
4. User is redirected to a status page

**Job lifecycle:**
1. Worker polls Postgres (or listens on NOTIFY channel) for queued jobs
2. Picks one up, marks `status=running`
3. Downloads source from R2 to local temp storage
4. Calls `pipeline.run_pipeline()`
5. Uploads resulting bundle to R2
6. Marks `status=succeeded` with bundle URL
7. (Future) Sends email notification

**Download bundle:**
1. User's status page polls `/jobs/{id}` periodically
2. On success, shows download link backed by signed R2 URL
3. User downloads zip and proceeds as in 7.1 step "Apply in Maya"

---

## 8. Quality strategy

### 8.1 Acceptance criteria for v1

A v1 release is acceptable when:

- [ ] `python pipeline.py test.mp4 -o ./jobs/smoketest --no-interpolate` completes in <60s on a modest laptop
- [ ] Same with interpolation completes in <10 min on the same hardware
- [ ] Output bundle contains exactly the four expected files
- [ ] CSV opens cleanly in a spreadsheet; all 52 ARKit columns present; no NaN values
- [ ] Preview MP4 plays cleanly; mesh overlay tracks face on most frames
- [ ] Apply script runs in Maya without errors on a standard ARKit-named rig
- [ ] Animator review (with at least one real animator) confirms output is "usable as a baseline"

### 8.2 Acceptance criteria for v2 (hosted)

- [ ] All v1 criteria still pass through the hosted path
- [ ] Job success rate ≥99% over 50+ test jobs
- [ ] No data leaks between jobs (signed URLs, bucket policies, etc.)
- [ ] Cleanup job removes expired artifacts on schedule
- [ ] Operational cost matches projections (FR 5.4)

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
- Same test videos, run through hosted API
- Verify identical CSV output (modulo timestamp differences)
- Verify queue ordering, timeout handling, error recovery

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

- **Channel-aware smoothing:** different filter parameters for brow vs. lip vs. eye channels (brows are noisier, lips need responsiveness)
- **Per-target scaling in apply script:** user-configurable scale per blendshape (e.g., boost eye blinks 1.8x)
- **Noise floor threshold:** clip channel values below threshold to zero (kills idle jitter)
- **Frame-rate-aware filter retuning:** more sophisticated than current 2-tier (24fps/60fps)
- **Optional per-actor calibration:** user supplies a neutral frame + ROM clip, system computes per-channel scaling factors

### 9.2 v2 (hosted launch)

- All FR-11 through FR-13 above
- Synchronous fallback for `--no-interpolate` mode (fast path returns in <60s)

### 9.3 v2.1+ (post-launch hosted improvements)

- Per-studio accounts and login
- Usage quotas and rate limiting
- Job history with re-download
- Email notification on completion
- Web-based preview viewer (no download needed to inspect quality)

### 9.4 v3 (quality escalation, if user demand justifies it)

- **RIFE integration via paid GPU:** Replicate.com endpoint integration, charge per-call. Improves interpolation quality significantly.
- **EMOCA solver as premium tier:** FLAME-based facial reconstruction. Requires GPU compute (rented) and FLAME commercial licensing (real cost). Premium pricing.
- **Custom rig retargeting:** non-ARKit-named rigs supported via uploaded mapping JSON or learned mapping
- **Faceware-tier paid software comparison:** explicit "use this when budget allows" guidance

### 9.5 Explicitly not on roadmap

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
| MediaPipe quality is insufficient for any real use case | Medium | High | Validate with one real animator on real clips before further investment; if quality is unacceptable, the entire premise needs reconsidering before building hosted layer |
| ffmpeg minterpolate is too slow for hosted CPU constraints | Low | Medium | Already known to take ~3 min for 5s clip; if jobs feel too long, add `--no-interpolate` as default and offer interpolation as premium tier |
| Maya rig variation breaks apply script | Medium | Medium | Apply script designed to handle multi-node ARKit splits; document common rig types and add explicit support per template |
| MediaPipe API changes in future versions | Low | Low | Pin version range in requirements.txt; bundle topology constants locally so newer MediaPipe doesn't break our preview |
| Railway service interruption | Low | Medium | Document local pipeline as fallback; users with critical work can run locally |

### 10.2 Product risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Users expect Faceware-quality output | High | High | Documentation strongly frames product as "baseline for animator polish"; provide quality samples on landing page |
| Hosted version cannibalizes local CLI without revenue | Medium | Medium | v2 launches with usage limits; clear path to paid tier exists for higher volume / better quality |
| Small studio audience too narrow to sustain product | Medium | High | Defer hosting investment until local CLI demonstrates real adoption; this PRD's v2 is contingent on v1 traction |
| ARKit-only is too restrictive | Medium | Medium | Custom rig mapping already exists in `retarget.py`; can be surfaced in apply script in v1.x if user demand emerges |

### 10.3 Operational risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| User uploads inappropriate content (deepfake material, etc.) | Medium | High | Terms of service prohibit unauthorized use of likeness; reactive moderation policy; no proactive scanning in v1 |
| Storage costs balloon | Low | Medium | Aggressive retention policy (7 days default); per-studio quotas |
| Spam / abuse of free tier | Medium | Low | Rate limit per IP and per account; require account for hosted v2 |

---

## 11. Open questions

These need decisions before v2 implementation:

1. **Authentication for hosted v2:** simple shared key, per-studio password auth, or full account system? (Recommendation: shared key for v2.0 if studio is single tenant; account system in v2.1)
2. **Pricing model:** free, freemium, per-job, monthly subscription? Depends on usage patterns from local CLI adoption.
3. **Frontend stack:** plain HTML form, simple React, or integration with an existing studio web app? Depends on studio's preferences.
4. **Hosting provider final choice:** Railway is the working assumption; alternatives (Fly.io, Render) similar. Decision deferred until v2 build starts.
5. **Email integration for notifications:** Resend (consistent with other projects) or skip in v2.0 and add later?

---

## 12. Glossary

- **ARKit-52:** Apple's standard set of 52 facial blendshape names used in iOS face tracking and widely adopted across the industry. The canonical "ARKit names" set.
- **Blendshape:** A named morph target on a mesh that, when weighted from 0 to 1, deforms the mesh toward a specific facial expression (e.g., "mouthSmileLeft").
- **MediaPipe:** Google's open-source ML framework. The Face Landmarker model emits ARKit-52 weights and 478-point facial landmarks.
- **MediaPipe Face Mesh:** The 478-point landmark topology that MediaPipe places on the face. Triangle-based, fixed connectivity.
- **One Euro filter:** Adaptive low-pass filter (Casiez et al, 2012) that smooths slow motion aggressively while preserving fast motion. Standard tool for face/hand tracking output.
- **FLAME:** A research-grade anatomical face model (Max Planck Institute) used by higher-quality solvers like EMOCA. Free for non-commercial use.
- **Markerless capture:** Facial capture that uses only the video image, no physical markers on the performer.
- **Retargeting:** Mapping animation data from one rig/format to another (here: ARKit-52 → custom rig blendshape names).
- **CRF / minterpolate / mci / aobmc:** ffmpeg-specific parameters. CRF controls H.264 quality; minterpolate is the temporal interpolation filter; mci/aobmc are its mode flags.
- **WSL2:** Windows Subsystem for Linux v2, the typical Linux environment running on a Windows host (relevant for the local pipeline's intended environment).
- **R2:** Cloudflare's S3-compatible object storage. Cheaper than S3 for egress.
- **Railway:** A Heroku-like platform-as-a-service. The intended host for the v2 service.
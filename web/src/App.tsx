import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBundle, fetchJob, HttpError, uploadJob } from "./api";
import type { JobDetail } from "./types";
import { TERMINAL_STATUSES } from "./types";

const POLL_INTERVAL_MS = 2000;

// Hard client-side constraints. The pipeline assumes these and the page
// rejects anything else before bytes hit the network.
const REQUIRED_WIDTH = 1920;
const REQUIRED_HEIGHT = 1080;
const MAX_DURATION_SECONDS = 7;
// Mirror of the backend `max_upload_bytes` (FR-12). Checked client-side
// so an oversized file is rejected before the upload starts, not after
// the server returns 413 mid/post-transfer.
const MAX_FILE_BYTES = 500 * 1024 * 1024;
const ALLOWED_MIME = "video/mp4";
const ALLOWED_EXTENSION = ".mp4";

// --- State machine --------------------------------------------------------
type AppState =
  | { kind: "idle" }
  | { kind: "validating"; file: File }
  | { kind: "uploading"; file: File; progress: number }
  | { kind: "polling"; jobId: string; job: JobDetail | null }
  | { kind: "succeeded"; job: JobDetail }
  | { kind: "failed"; job: JobDetail }
  | { kind: "error"; message: string; jobId: string | null };

interface VideoMeta {
  width: number;
  height: number;
  duration: number;
}

async function probeVideoMeta(file: File): Promise<VideoMeta> {
  const url = URL.createObjectURL(file);
  try {
    return await new Promise<VideoMeta>((resolve, reject) => {
      const video = document.createElement("video");
      video.preload = "metadata";
      video.muted = true;
      video.addEventListener("loadedmetadata", () => {
        if (!Number.isFinite(video.duration)) {
          reject(new Error("Could not read video duration."));
          return;
        }
        resolve({
          width: video.videoWidth,
          height: video.videoHeight,
          duration: video.duration,
        });
      });
      video.addEventListener("error", () => {
        reject(new Error("Could not decode video metadata."));
      });
      video.src = url;
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

function validationError(file: File, meta: VideoMeta): string | null {
  if (meta.width !== REQUIRED_WIDTH || meta.height !== REQUIRED_HEIGHT) {
    return `Video must be exactly ${REQUIRED_WIDTH}×${REQUIRED_HEIGHT}. Got ${meta.width}×${meta.height}.`;
  }
  if (meta.duration > MAX_DURATION_SECONDS + 0.05) {
    return `Video must be ${MAX_DURATION_SECONDS} seconds or shorter. Got ${meta.duration.toFixed(2)}s.`;
  }
  if (
    file.type !== ALLOWED_MIME &&
    !file.name.toLowerCase().endsWith(ALLOWED_EXTENSION)
  ) {
    return "Video must be an .mp4 file.";
  }
  if (file.size > MAX_FILE_BYTES) {
    return `File is too large (${formatBytes(file.size)}). Maximum is ${formatBytes(MAX_FILE_BYTES)}.`;
  }
  return null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

export function App(): React.JSX.Element {
  const [state, setState] = useState<AppState>({ kind: "idle" });
  const [dragOver, setDragOver] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Aborts the in-flight XHR upload on reset/unmount so a discarded
  // upload doesn't keep consuming a server pipeline slot.
  const uploadControllerRef = useRef<AbortController | null>(null);

  // Only the job id should retrigger the poll loop, not every `state`
  // object identity change (each poll tick produces a new state object
  // with the same id).
  const pollingJobId = state.kind === "polling" ? state.jobId : null;

  // Abort any in-flight upload if the component goes away.
  useEffect(() => {
    return () => uploadControllerRef.current?.abort();
  }, []);

  // --- Polling loop ------------------------------------------------------
  useEffect(() => {
    if (pollingJobId === null) return;

    const controller = new AbortController();
    // A ref-backed flag so StrictMode's mount→unmount→remount can't
    // leave a tick from the first run writing state after cleanup.
    const live = { current: true };

    const tick = async (): Promise<void> => {
      try {
        const job = await fetchJob(pollingJobId, controller.signal);
        if (!live.current) return;
        if (TERMINAL_STATUSES.has(job.status)) {
          setState(
            job.status === "succeeded"
              ? { kind: "succeeded", job }
              : { kind: "failed", job },
          );
        } else {
          setState({ kind: "polling", jobId: job.id, job });
        }
      } catch (err: unknown) {
        if (!live.current) return;
        if (err instanceof HttpError && err.status === 404) {
          setState({
            kind: "error",
            message: "Job no longer exists on the server.",
            jobId: pollingJobId,
          });
          return;
        }
        // eslint-disable-next-line no-console
        console.warn("poll failed; will retry", err);
      }
    };

    void tick();
    const interval = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      live.current = false;
      controller.abort();
      window.clearInterval(interval);
    };
  }, [pollingJobId]);

  const handleFile = useCallback(async (file: File): Promise<void> => {
    setState({ kind: "validating", file });
    let meta: VideoMeta;
    try {
      meta = await probeVideoMeta(file);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setState({ kind: "error", message, jobId: null });
      return;
    }

    const reason = validationError(file, meta);
    if (reason) {
      setState({ kind: "error", message: reason, jobId: null });
      return;
    }

    setState({ kind: "uploading", file, progress: 0 });
    uploadControllerRef.current?.abort();
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    try {
      const created = await uploadJob({
        file,
        signal: controller.signal,
        onProgress: (fraction) =>
          setState({ kind: "uploading", file, progress: fraction }),
      });
      uploadControllerRef.current = null;
      setState({ kind: "polling", jobId: created.id, job: null });
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (err: unknown) {
      const message =
        err instanceof HttpError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Upload failed.";
      setState({ kind: "error", message, jobId: null });
    }
  }, []);

  const onFileInput = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const picked = event.target.files?.[0] ?? null;
    if (picked) void handleFile(picked);
  };

  const onDrop = (event: React.DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    setDragOver(false);
    const dropped = event.dataTransfer.files?.[0] ?? null;
    if (dropped) void handleFile(dropped);
  };

  const handleDownload = useCallback(async (): Promise<void> => {
    if (state.kind !== "succeeded" || downloading) return;
    setDownloading(true);
    try {
      const bundle = await fetchBundle(state.job.id);
      // The URL is either an absolute https presigned URL (R2 backend)
      // or a same-origin relative path (LocalStorage backend, e.g.
      // "/storage/bundles/<id>.zip?..."). Resolve against the current
      // origin and only navigate if it lands on http(s) — this still
      // blocks javascript:/data: URLs without rejecting the valid
      // relative LocalStorage form.
      let resolved: URL;
      try {
        resolved = new URL(bundle.url, window.location.origin);
      } catch {
        throw new Error("Server returned an unexpected download URL.");
      }
      if (resolved.protocol !== "https:" && resolved.protocol !== "http:") {
        throw new Error("Server returned an unexpected download URL.");
      }
      window.location.href = resolved.href;
    } catch (err: unknown) {
      const message =
        err instanceof HttpError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Download failed.";
      setState({ kind: "error", message, jobId: state.job.id });
    } finally {
      setDownloading(false);
    }
  }, [state, downloading]);

  const handleReset = (): void => {
    uploadControllerRef.current?.abort();
    uploadControllerRef.current = null;
    if (fileInputRef.current) fileInputRef.current.value = "";
    setState({ kind: "idle" });
  };

  const dropEnabled = state.kind === "idle" || state.kind === "error";

  return (
    <main className="app">
      <header>
        <p className="eyebrow">monocular video → ARKit-52</p>
        <div className="titleline">
          <h1>FaceCap</h1>
          <span className="tag">baseline</span>
        </div>
        <p className="sub">
          Drop a {REQUIRED_WIDTH}×{REQUIRED_HEIGHT} mp4 (≤
          {MAX_DURATION_SECONDS}s). Processing runs server-side; a Maya-ready
          bundle downloads when it&apos;s ready.
        </p>
      </header>

      {dropEnabled && (
        <section
          className={`dropzone ${dragOver ? "dropzone--over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              fileInputRef.current?.click();
            }
          }}
        >
          <p className="dropzone__primary">Drop video here</p>
          <p className="dropzone__secondary">or click to choose a file</p>
          <p className="dropzone__hint">
            {REQUIRED_WIDTH}×{REQUIRED_HEIGHT} · ≤{MAX_DURATION_SECONDS}s · .mp4
          </p>
          <input
            ref={fileInputRef}
            type="file"
            accept={ALLOWED_MIME + "," + ALLOWED_EXTENSION}
            onChange={onFileInput}
            hidden
          />
        </section>
      )}

      {state.kind === "validating" && (
        <section className="card">
          <div className="card__head">
            <h2>Checking video</h2>
          </div>
          <div className="processing" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            <span className="processing__label">Probing resolution and duration…</span>
          </div>
          <p className="muted mono">
            {state.file.name} · {formatBytes(state.file.size)}
          </p>
        </section>
      )}

      {state.kind === "uploading" && (
        <section className="card">
          <div className="card__head">
            <h2>Uploading</h2>
            <span className="pill pill--running">
              {Math.round(state.progress * 100)}%
            </span>
          </div>
          <p className="muted mono">
            {state.file.name} · {formatBytes(state.file.size)}
          </p>
          <progress
            value={state.progress}
            max={1}
            aria-label={`Upload progress: ${Math.round(state.progress * 100)}%`}
          />
        </section>
      )}

      {state.kind === "polling" && (
        <section className="card">
          <div className="card__head">
            <h2>
              {state.job?.status === "running" ? "Processing" : "Queued"}
            </h2>
            <span
              className={`pill pill--${state.job?.status ?? "queued"}`}
            >
              {state.job?.status ?? "queued"}
            </span>
          </div>
          <dl className="meta">
            <dt>job id</dt>
            <dd>{state.jobId}</dd>
          </dl>
          <div className="processing" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            <span className="processing__label">
              {state.job?.status === "running"
                ? "Running the capture pipeline…"
                : "Waiting for a worker…"}
            </span>
          </div>
          <p className="muted">
            This can take 5 minutes or more. Polling every{" "}
            {POLL_INTERVAL_MS / 1000}s — keep this tab open.
          </p>
        </section>
      )}

      {state.kind === "succeeded" && (
        <section className="card success">
          <div className="card__head">
            <h2>Bundle ready</h2>
            <span className="pill pill--succeeded">succeeded</span>
          </div>
          <dl className="meta">
            <dt>job id</dt>
            <dd>{state.job.id}</dd>
            {state.job.source_duration_seconds != null && (
              <>
                <dt>source</dt>
                <dd>{state.job.source_duration_seconds.toFixed(2)}s</dd>
              </>
            )}
            <dt>contents</dt>
            <dd>
              preview.mp4 · blendshapes.csv · apply_in_maya.py ·
              apply_in_blender.py · README.txt
            </dd>
          </dl>
          <div className="row">
            <button
              type="button"
              onClick={() => void handleDownload()}
              disabled={downloading}
            >
              {downloading ? "Preparing…" : "Download bundle"}
            </button>
            <button type="button" className="ghost" onClick={handleReset}>
              New video
            </button>
          </div>
        </section>
      )}

      {state.kind === "failed" && (
        <section className="card failure">
          <div className="card__head">
            <h2>Processing failed</h2>
            <span className="pill pill--failed">failed</span>
          </div>
          {state.job.error_log && (
            <pre className="error-log">{state.job.error_log}</pre>
          )}
          <button type="button" className="ghost" onClick={handleReset}>
            New video
          </button>
        </section>
      )}

      {state.kind === "error" && (
        <section className="card failure">
          <div className="card__head">
            <h2>Error</h2>
            <span className="pill pill--failed">error</span>
          </div>
          <p className="muted">{state.message}</p>
          <button type="button" className="ghost" onClick={handleReset}>
            Try again
          </button>
        </section>
      )}

      <p className="footnote">
        <strong>Built for ARKit-standard rigs.</strong> The bundle is
        ARKit-52 blendshape data plus a Maya apply script, intended for a
        mesh with ARKit blendshape naming (ReadyPlayerMe, MetaHuman, or an
        in-house ARKit rig). <strong>It&apos;s a baseline, not finished
        animation</strong> — a starting point for an animator to polish, so
        expect brow drift and muted micro-expressions. No storage: job state
        is in-memory and bundles auto-expire.
      </p>
    </main>
  );
}

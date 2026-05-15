import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBundle, fetchJob, HttpError, uploadJob } from "./api";
import type { JobDetail } from "./types";
import { TERMINAL_STATUSES } from "./types";

const POLL_INTERVAL_MS = 2000;

// Hard client-side constraints. The pipeline assumes these and the page
// rejects anything else before bytes hit the network.
const REQUIRED_WIDTH = 1920;
const REQUIRED_HEIGHT = 1080;
const MAX_DURATION_SECONDS = 5;
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
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // --- Polling loop ------------------------------------------------------
  useEffect(() => {
    if (state.kind !== "polling") return;

    const controller = new AbortController();
    let cancelled = false;

    const tick = async (): Promise<void> => {
      try {
        const job = await fetchJob(state.jobId, controller.signal);
        if (cancelled) return;
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
        if (cancelled) return;
        if (err instanceof HttpError && err.status === 404) {
          setState({
            kind: "error",
            message: "Job no longer exists on the server.",
            jobId: state.jobId,
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
      cancelled = true;
      controller.abort();
      window.clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.kind === "polling" ? state.jobId : null]);

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
    try {
      const created = await uploadJob({
        file,
        onProgress: (fraction) =>
          setState({ kind: "uploading", file, progress: fraction }),
      });
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
    if (state.kind !== "succeeded") return;
    try {
      const bundle = await fetchBundle(state.job.id);
      window.location.href = bundle.url;
    } catch (err: unknown) {
      const message =
        err instanceof HttpError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Download failed.";
      setState({ kind: "error", message, jobId: state.job.id });
    }
  }, [state]);

  const handleReset = (): void => {
    if (fileInputRef.current) fileInputRef.current.value = "";
    setState({ kind: "idle" });
  };

  const dropEnabled = state.kind === "idle" || state.kind === "error";

  return (
    <main className="app">
      <header>
        <h1>face-capture</h1>
        <p className="sub">
          Drop a {REQUIRED_WIDTH}×{REQUIRED_HEIGHT} mp4 (≤
          {MAX_DURATION_SECONDS}s). Processing runs server-side; the
          deliverable bundle downloads when it&apos;s ready.
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
          <h2>Checking video</h2>
          <p className="muted">
            {state.file.name} · {formatBytes(state.file.size)}
          </p>
        </section>
      )}

      {state.kind === "uploading" && (
        <section className="card">
          <h2>Uploading</h2>
          <p className="muted">
            {state.file.name} · {formatBytes(state.file.size)}
          </p>
          <progress value={state.progress} max={1} />
          <p className="muted">{Math.round(state.progress * 100)}%</p>
        </section>
      )}

      {state.kind === "polling" && (
        <section className="card">
          <h2>{state.job?.status === "running" ? "Processing" : "Queued"}</h2>
          <p className="muted">
            Job id: <code>{state.jobId}</code>
          </p>
          <p className="muted">Polling every {POLL_INTERVAL_MS / 1000}s…</p>
        </section>
      )}

      {state.kind === "succeeded" && (
        <section className="card success">
          <h2>Done</h2>
          {state.job.source_duration_seconds != null && (
            <p className="muted">
              Source duration: {state.job.source_duration_seconds.toFixed(2)}s
            </p>
          )}
          <div className="row">
            <button type="button" onClick={() => void handleDownload()}>
              Download bundle
            </button>
            <button type="button" className="ghost" onClick={handleReset}>
              New video
            </button>
          </div>
        </section>
      )}

      {state.kind === "failed" && (
        <section className="card failure">
          <h2>Processing failed</h2>
          {state.job.error_log && (
            <pre className="error-log">{state.job.error_log}</pre>
          )}
          <button type="button" onClick={handleReset}>
            New video
          </button>
        </section>
      )}

      {state.kind === "error" && (
        <section className="card failure">
          <h2>Error</h2>
          <p>{state.message}</p>
          <button type="button" onClick={handleReset}>
            Try again
          </button>
        </section>
      )}
    </main>
  );
}

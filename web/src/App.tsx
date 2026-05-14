import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchBundle, fetchJob, HttpError, uploadJob } from "./api";
import type { JobDetail } from "./types";
import { TERMINAL_STATUSES } from "./types";

// --- Persistence ----------------------------------------------------------
// The API key lives in localStorage so the user doesn't have to paste it
// every reload; it is a shared secret, not a long-lived credential. A
// reload while polling resumes the same job by stashing its id too.
const API_KEY_STORAGE = "face-capture.apiKey";
const ACTIVE_JOB_STORAGE = "face-capture.activeJobId";

const POLL_INTERVAL_MS = 2000;
const ALLOWED_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"];

// --- State machine --------------------------------------------------------
type AppState =
  | { kind: "idle" }
  | { kind: "uploading"; file: File; progress: number }
  | { kind: "polling"; jobId: string; job: JobDetail | null }
  | { kind: "succeeded"; job: JobDetail }
  | { kind: "failed"; job: JobDetail }
  | { kind: "error"; message: string; jobId: string | null };

function loadStored(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function storeOrClear(key: string, value: string): void {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    // Storage is best-effort; ignore quota / privacy-mode failures.
  }
}

function fileHasAllowedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

export function App(): React.JSX.Element {
  const [apiKey, setApiKey] = useState<string>(() => loadStored(API_KEY_STORAGE));
  const [state, setState] = useState<AppState>(() => {
    const resumeId = loadStored(ACTIVE_JOB_STORAGE);
    return resumeId
      ? { kind: "polling", jobId: resumeId, job: null }
      : { kind: "idle" };
  });
  const [file, setFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Persist the active job id whenever it changes.
  useEffect(() => {
    const id =
      state.kind === "polling" || state.kind === "succeeded" || state.kind === "failed"
        ? state.job?.id ?? (state.kind === "polling" ? state.jobId : null)
        : null;
    storeOrClear(ACTIVE_JOB_STORAGE, id ?? "");
  }, [state]);

  // Persist the API key on every change.
  useEffect(() => {
    storeOrClear(API_KEY_STORAGE, apiKey);
  }, [apiKey]);

  // --- Polling loop ------------------------------------------------------
  useEffect(() => {
    if (state.kind !== "polling") return;
    if (!apiKey) {
      setState({
        kind: "error",
        message: "API key required to resume polling.",
        jobId: state.jobId,
      });
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    const tick = async (): Promise<void> => {
      try {
        const job = await fetchJob(apiKey, state.jobId, controller.signal);
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
        // Transient errors: keep the existing state, log and retry.
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
    // Re-run only when the polled job id or apiKey changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.kind === "polling" ? state.jobId : null, apiKey]);

  // --- Handlers ----------------------------------------------------------
  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const picked = event.target.files?.[0] ?? null;
    if (picked && !fileHasAllowedExtension(picked.name)) {
      setState({
        kind: "error",
        message: `Unsupported file extension. Allowed: ${ALLOWED_EXTENSIONS.join(", ")}.`,
        jobId: null,
      });
      setFile(null);
      return;
    }
    setFile(picked);
  };

  const handleUpload = useCallback(async (): Promise<void> => {
    if (!file || !apiKey) return;
    setState({ kind: "uploading", file, progress: 0 });
    try {
      const created = await uploadJob({
        apiKey,
        file,
        onProgress: (fraction) =>
          setState({ kind: "uploading", file, progress: fraction }),
      });
      setState({ kind: "polling", jobId: created.id, job: null });
      setFile(null);
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
  }, [apiKey, file]);

  const handleDownload = useCallback(async (): Promise<void> => {
    if (state.kind !== "succeeded") return;
    try {
      const bundle = await fetchBundle(apiKey, state.job.id);
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
  }, [apiKey, state]);

  const handleReset = (): void => {
    setFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    setState({ kind: "idle" });
  };

  const canUpload = useMemo(
    () => state.kind === "idle" && !!file && !!apiKey.trim(),
    [state.kind, file, apiKey],
  );

  // --- Render ------------------------------------------------------------
  return (
    <main className="app">
      <header>
        <h1>face-capture</h1>
        <p className="sub">
          Upload a video, the pipeline runs server-side, download the deliverables when it
          finishes.
        </p>
      </header>

      <section className="card">
        <label className="field">
          <span>API key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="shared secret"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
      </section>

      {state.kind === "idle" && (
        <section className="card">
          <label className="field">
            <span>Source video</span>
            <input
              ref={fileInputRef}
              type="file"
              accept={ALLOWED_EXTENSIONS.join(",")}
              onChange={handleFileChange}
              disabled={!apiKey.trim()}
            />
          </label>
          {file && (
            <p className="muted">
              {file.name} &middot; {formatBytes(file.size)}
            </p>
          )}
          <button type="button" disabled={!canUpload} onClick={() => void handleUpload()}>
            Upload &amp; queue job
          </button>
        </section>
      )}

      {state.kind === "uploading" && (
        <section className="card">
          <h2>Uploading</h2>
          <p className="muted">
            {state.file.name} &middot; {formatBytes(state.file.size)}
          </p>
          <progress value={state.progress} max={1} />
          <p className="muted">{Math.round(state.progress * 100)}%</p>
        </section>
      )}

      {state.kind === "polling" && (
        <section className="card">
          <h2>{state.job?.status === "running" ? "Running" : "Queued"}</h2>
          <p className="muted">Job id: <code>{state.jobId}</code></p>
          {state.job?.started_at && (
            <p className="muted">Started: {state.job.started_at}</p>
          )}
          <p className="muted">Polling every {POLL_INTERVAL_MS / 1000}s…</p>
        </section>
      )}

      {state.kind === "succeeded" && (
        <section className="card success">
          <h2>Succeeded</h2>
          <p className="muted">Job id: <code>{state.job.id}</code></p>
          {state.job.source_duration_seconds != null && (
            <p className="muted">
              Source duration: {state.job.source_duration_seconds.toFixed(2)}s
            </p>
          )}
          {state.job.expires_at && (
            <p className="muted">Bundle expires: {state.job.expires_at}</p>
          )}
          <div className="row">
            <button type="button" onClick={() => void handleDownload()}>
              Download bundle
            </button>
            <button type="button" className="ghost" onClick={handleReset}>
              New job
            </button>
          </div>
        </section>
      )}

      {state.kind === "failed" && (
        <section className="card failure">
          <h2>Failed</h2>
          <p className="muted">Job id: <code>{state.job.id}</code></p>
          {state.job.error_log && (
            <pre className="error-log">{state.job.error_log}</pre>
          )}
          <button type="button" onClick={handleReset}>
            New job
          </button>
        </section>
      )}

      {state.kind === "error" && (
        <section className="card failure">
          <h2>Error</h2>
          <p>{state.message}</p>
          {state.jobId && <p className="muted">Job id: <code>{state.jobId}</code></p>}
          <button type="button" onClick={handleReset}>
            Reset
          </button>
        </section>
      )}
    </main>
  );
}

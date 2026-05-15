import type {
  ApiError,
  BundleDownload,
  JobCreated,
  JobDetail,
  JobStatus,
} from "./types";

const VALID_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "queued",
  "running",
  "succeeded",
  "failed",
]);

export class HttpError extends Error implements ApiError {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function parseError(response: Response): Promise<HttpError> {
  let detail = response.statusText;
  try {
    const body: unknown = await response.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      detail = (body as { detail: string }).detail;
    }
  } catch {
    // Body wasn't JSON; fall back to statusText.
  }
  return new HttpError(response.status, detail);
}

function asJobStatus(value: unknown): JobStatus {
  if (typeof value === "string" && VALID_STATUSES.has(value as JobStatus)) {
    return value as JobStatus;
  }
  throw new Error(`Invalid job status from server: ${String(value)}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown, field: string): string {
  if (typeof value === "string") return value;
  throw new Error(`Expected string for ${field}, got ${typeof value}`);
}

function asNumber(value: unknown, field: string): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  throw new Error(`Expected number for ${field}, got ${typeof value}`);
}

function asStringOrNull(value: unknown, field: string): string | null {
  if (value === null || value === undefined) return null;
  return asString(value, field);
}

function asNumberOrNull(value: unknown, field: string): number | null {
  if (value === null || value === undefined) return null;
  return asNumber(value, field);
}

function parseJobDetail(body: unknown): JobDetail {
  if (!isRecord(body)) throw new Error("Expected JobDetail object.");
  return {
    id: asString(body.id, "id"),
    status: asJobStatus(body.status),
    created_at: asString(body.created_at, "created_at"),
    started_at: asStringOrNull(body.started_at, "started_at"),
    completed_at: asStringOrNull(body.completed_at, "completed_at"),
    error_log: asStringOrNull(body.error_log, "error_log"),
    source_duration_seconds: asNumberOrNull(
      body.source_duration_seconds,
      "source_duration_seconds",
    ),
  };
}

function parseJobCreated(body: unknown): JobCreated {
  if (!isRecord(body)) throw new Error("Expected JobCreated object.");
  return {
    id: asString(body.id, "id"),
    status: asJobStatus(body.status),
    created_at: asString(body.created_at, "created_at"),
  };
}

function parseBundleDownload(body: unknown): BundleDownload {
  if (!isRecord(body)) throw new Error("Expected BundleDownload object.");
  return {
    url: asString(body.url, "url"),
    expires_in: asNumber(body.expires_in, "expires_in"),
  };
}

export interface UploadOptions {
  file: File;
  onProgress?: (fractionComplete: number) => void;
  signal?: AbortSignal;
}

// XMLHttpRequest is the only browser API that exposes upload progress
// events; fetch() does not.
export function uploadJob(opts: UploadOptions): Promise<JobCreated> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("video", opts.file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");

    if (opts.onProgress && xhr.upload) {
      xhr.upload.addEventListener("progress", (event: ProgressEvent) => {
        if (event.lengthComputable && opts.onProgress) {
          opts.onProgress(event.loaded / event.total);
        }
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const parsed: unknown = JSON.parse(xhr.responseText);
          resolve(parseJobCreated(parsed));
        } catch (err) {
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      } else {
        let detail = xhr.statusText;
        try {
          const body: unknown = JSON.parse(xhr.responseText);
          if (
            isRecord(body) &&
            "detail" in body &&
            typeof body.detail === "string"
          ) {
            detail = body.detail;
          }
        } catch {
          // Response wasn't JSON.
        }
        reject(new HttpError(xhr.status, detail));
      }
    });
    xhr.addEventListener("error", () => {
      reject(new HttpError(0, "Network error during upload."));
    });
    xhr.addEventListener("abort", () => {
      reject(new HttpError(0, "Upload aborted."));
    });

    if (opts.signal) {
      if (opts.signal.aborted) {
        xhr.abort();
      } else {
        opts.signal.addEventListener("abort", () => xhr.abort());
      }
    }

    xhr.send(form);
  });
}

export async function fetchJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobDetail> {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    signal,
  });
  if (!response.ok) throw await parseError(response);
  const body: unknown = await response.json();
  return parseJobDetail(body);
}

export async function fetchBundle(jobId: string): Promise<BundleDownload> {
  const response = await fetch(
    `/api/jobs/${encodeURIComponent(jobId)}/bundle`,
  );
  if (!response.ok) throw await parseError(response);
  const body: unknown = await response.json();
  return parseBundleDownload(body);
}

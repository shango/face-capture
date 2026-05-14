// Mirror of backend Pydantic models. Kept narrow on purpose: a typo here
// will surface as a runtime parse error in api.ts, not a silent `any`.

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "succeeded",
  "failed",
]);

export interface JobCreated {
  id: string;
  status: JobStatus;
  created_at: string;
}

export interface JobDetail {
  id: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_log: string | null;
  source_duration_seconds: number | null;
  expires_at: string | null;
}

export interface BundleDownload {
  url: string;
  expires_in: number;
}

export interface ApiError {
  status: number;
  detail: string;
}

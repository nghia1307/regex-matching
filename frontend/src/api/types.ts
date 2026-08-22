export type JobStatus = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'CANCELLED';

export type OperationName = 'REPLACE' | 'MASK' | 'EXTRACT' | 'VALIDATE';

export interface S3File {
  key: string;
  name: string;
  size: number;
  size_human: string;
  extension: string;
  last_modified: string | null;
}

export interface FileListResponse {
  bucket: string;
  prefix: string;
  count: number;
  files: S3File[];
}

export interface FilePreview {
  key: string;
  columns: string[];
  sample_rows: Record<string, string>[];
  truncated: boolean;
  sheet_names: string[];
  note: string;
  cached?: boolean;
}

export interface OperationInfo {
  value: OperationName;
  label: string;
  needs_replacement: boolean;
  creates_column: boolean;
}

export interface RegexInfo {
  pattern: string;
  case_insensitive: boolean;
  replacement_template: string;
  group: number;
  provider: string;
  model: string;
  explanation: string;
  confidence: number;
  cached: boolean;
  warnings: string[];
  self_test_passed: boolean | null;
}

export interface Job {
  id: string;
  status: JobStatus;
  progress: number;
  phase: string;
  source_key: string;
  sheet_name: string;
  operation: OperationName;
  natural_language: string;
  replacement_value: string;
  target_columns: string[];
  regex: RegexInfo;
  result_columns: string[];
  added_columns: string[];
  total_rows: number;
  matched_cells: number;
  error_message: string;
  error_type: string;
  attempt: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  queue_wait_seconds: number | null;
}

export interface JobSummary {
  id: string;
  status: JobStatus;
  progress: number;
  operation: OperationName;
  source_key: string;
  natural_language: string;
  total_rows: number;
  matched_cells: number;
  created_at: string;
  duration_seconds: number | null;
}

export interface ResultPage {
  job_id: string;
  columns: string[];
  added_columns: string[];
  rows: Record<string, string | number | boolean | null>[];
  page: number;
  page_size: number;
  total_rows: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
  row_offset: number;
  matched_cells: number;
  regex: string;
  operation: OperationName;
}

export interface CreateJobPayload {
  source_key: string;
  sheet_name?: string;
  operation: OperationName;
  natural_language: string;
  replacement_value?: string;
  target_columns: string[];
  force_refresh?: boolean;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

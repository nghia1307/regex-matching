import type { FilePreview, Job, OperationInfo, ResultPage, S3File } from '../api/types';

/**
 * Builders for API payloads. Each takes a partial override so a test states only
 * the fields it actually cares about and the rest stays realistic.
 */

export function makeFile(overrides: Partial<S3File> = {}): S3File {
  return {
    key: 'raw/customers.csv',
    name: 'customers.csv',
    size: 2048,
    size_human: '2.0 KB',
    extension: '.csv',
    last_modified: '2026-08-01T10:00:00Z',
    ...overrides,
  };
}

export function makePreview(overrides: Partial<FilePreview> = {}): FilePreview {
  return {
    key: 'raw/customers.csv',
    columns: ['id', 'email', 'city'],
    sample_rows: [
      { id: '1', email: 'ada@example.com', city: 'London' },
      { id: '2', email: 'grace@example.com', city: 'Baltimore' },
    ],
    truncated: false,
    sheet_names: [],
    note: '',
    ...overrides,
  };
}

export const OPERATIONS: OperationInfo[] = [
  { value: 'REPLACE', label: 'Replace matches with a fixed value', needs_replacement: true, creates_column: false },
  { value: 'MASK', label: 'Mask matches', needs_replacement: false, creates_column: false },
  { value: 'EXTRACT', label: 'Extract matches', needs_replacement: false, creates_column: true },
  { value: 'VALIDATE', label: 'Validate matches', needs_replacement: false, creates_column: true },
];

export function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: '11111111-2222-3333-4444-555555555555',
    status: 'RUNNING',
    progress: 40,
    phase: 'Applying the pattern',
    source_key: 'raw/customers.csv',
    sheet_name: '',
    operation: 'REPLACE',
    natural_language: 'Find email addresses and replace them',
    replacement_value: 'REDACTED',
    target_columns: ['email'],
    regex: {
      pattern: '[\w.]+@[\w.]+',
      case_insensitive: true,
      replacement_template: '',
      group: 0,
      provider: 'gemini',
      model: 'gemini-2.5-flash',
      explanation: 'Matches an email address.',
      confidence: 0.9,
      cached: false,
      warnings: [],
      self_test_passed: true,
    },
    result_columns: ['id', 'email', 'city'],
    added_columns: [],
    total_rows: 1000,
    matched_cells: 250,
    error_message: '',
    error_type: '',
    attempt: 1,
    created_at: '2026-08-01T10:00:00Z',
    started_at: '2026-08-01T10:00:01Z',
    finished_at: null,
    duration_seconds: null,
    queue_wait_seconds: 1,
    ...overrides,
  };
}

export function makeResultPage(overrides: Partial<ResultPage> = {}): ResultPage {
  return {
    job_id: '11111111-2222-3333-4444-555555555555',
    columns: ['id', 'email', 'email_valid'],
    added_columns: ['email_valid'],
    rows: [
      { id: 1, email: 'ada@example.com', email_valid: true },
      { id: 2, email: '', email_valid: false },
    ],
    page: 1,
    page_size: 50,
    total_rows: 120,
    total_pages: 3,
    has_next: true,
    has_previous: false,
    row_offset: 0,
    matched_cells: 42,
    regex: '[\w.]+@[\w.]+',
    operation: 'VALIDATE',
    ...overrides,
  };
}

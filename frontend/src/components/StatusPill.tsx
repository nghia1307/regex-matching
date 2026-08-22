import type { JobStatus } from '../api/types';

/** Domain component: the colour it picks is tied to the job lifecycle. */
export function StatusPill({ status }: { status: JobStatus }) {
  return <span className={`pill pill-${status.toLowerCase()}`}>{status}</span>;
}

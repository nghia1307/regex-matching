import { useEffect, useState } from 'react';
import { ApiError, api } from './api/client';
import type { CreateJobPayload, S3File } from './api/types';
import { FileBrowser } from './components/FileBrowser';
import { JobForm } from './components/JobForm';
import { JobHistory } from './components/JobHistory';
import { JobMonitor } from './components/JobMonitor';
import { ResultTable } from './components/ResultTable';
import { ErrorBanner, Step } from './components/ui';
import { useJobPolling } from './hooks/useJobPolling';

// In prod this is a relative path proxied by nginx (see nginx.conf) so it
// works via the EC2 IP, instance DNS, or the CloudFront domain alike. In dev
// there's no such proxy, so it falls back to Flower's own published port.
const FLOWER_URL = (import.meta.env.VITE_FLOWER_URL as string | undefined) ?? 'http://localhost:5555';
// The Spark UI is bound to 127.0.0.1 on the box and has no auth of its own --
// reachable only by SSH-tunnelling it to your own localhost (see deploy.sh),
// so localhost is the correct target in every environment, not a bug.
const SPARK_URL = (import.meta.env.VITE_SPARK_URL as string | undefined) ?? 'http://localhost:8080';

export default function App() {
  const [file, setFile] = useState<S3File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [historyToken, setHistoryToken] = useState(0);
  const [health, setHealth] = useState<string>('checking');

  const { job, error: pollError } = useJobPolling(jobId);

  useEffect(() => {
    api
      .health()
      .then((response) => setHealth(response.status))
      .catch(() => setHealth('unreachable'));
  }, []);

  // The result becomes readable only once the job succeeds; nudge the history.
  useEffect(() => {
    if (job && (job.status === 'SUCCESS' || job.status === 'FAILED' || job.status === 'CANCELLED')) {
      setHistoryToken((token) => token + 1);
      setCancelling(false);
    }
  }, [job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async (payload: CreateJobPayload) => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await api.createJob(payload);
      setJobId(created.id);
      setHistoryToken((token) => token + 1);
    } catch (cause) {
      setSubmitError(cause instanceof ApiError ? cause.message : 'Could not submit the job');
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async () => {
    if (!jobId) return;
    setCancelling(true);
    try {
      await api.cancelJob(jobId);
    } catch (cause) {
      setSubmitError(cause instanceof ApiError ? cause.message : 'Cancel failed');
      setCancelling(false);
    }
  };

  return (
    <div className="app">
      <header className="app-head">
        <div>
          <h1>Regex Pattern Matching &amp; Replacement</h1>
          <p className="muted">
            Describe a pattern in plain English; an LLM turns it into a regex and Spark applies it
            across the whole dataset.
          </p>
        </div>
        <div className="row-gap">
          <span className={`tag tag-health tag-health-${health}`}>API: {health}</span>
          <a className="btn btn-ghost" href={FLOWER_URL} target="_blank" rel="noreferrer">
            Flower
          </a>
          <a className="btn btn-ghost" href={SPARK_URL} target="_blank" rel="noreferrer" title="Requires an SSH tunnel to this port">
            Spark
          </a>
        </div>
      </header>

      <main className="layout">
        <div className="column">
          <Step index={1} title="Choose a file from the bucket" done={Boolean(file)}>
            <FileBrowser
              selected={file?.key ?? null}
              onSelect={(selected) => {
                setFile(selected);
                setJobId(null);
                setSubmitError(null);
              }}
            />
          </Step>

          {file && (
            <Step index={2} title="Describe the transformation" done={Boolean(jobId)}>
              {submitError && <ErrorBanner title="Submit failed" message={submitError} onDismiss={() => setSubmitError(null)} />}
              <JobForm file={file} submitting={submitting} onSubmit={submit} />
            </Step>
          )}

          {jobId && job && (
            <Step index={3} title="Job progress" done={job.status === 'SUCCESS'}>
              <JobMonitor job={job} pollError={pollError} cancelling={cancelling} onCancel={cancel} />
            </Step>
          )}

          {job?.status === 'SUCCESS' && (
            <Step index={4} title="Processed data" done>
              <ResultTable jobId={job.id} />
            </Step>
          )}
        </div>

        <JobHistory
          activeJobId={jobId}
          refreshToken={historyToken}
          onOpen={(id) => {
            setJobId(id);
            setSubmitError(null);
          }}
        />
      </main>

      <footer className="app-foot muted small">
        Django + Celery + Redis + PySpark + MinIO &middot; results are paged straight out of Parquet,
        so the browser never receives more than one page.
      </footer>
    </div>
  );
}

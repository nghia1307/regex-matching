interface Props {
  value: number;
  indeterminate?: boolean;
}

export function ProgressBar({ value, indeterminate }: Props) {
  return (
    <div className="progress" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}>
      <div
        className={`progress-fill${indeterminate ? ' progress-indeterminate' : ''}`}
        // A sliver of fill is always visible, so a 0% job still looks alive.
        style={{ width: `${Math.max(2, Math.min(value, 100))}%` }}
      />
    </div>
  );
}

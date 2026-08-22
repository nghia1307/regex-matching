import type { ReactNode } from 'react';

interface Props {
  index: number;
  title: string;
  children: ReactNode;
  done?: boolean;
}

/** One numbered card in the wizard; ticks over to a check mark when satisfied. */
export function Step({ index, title, children, done }: Props) {
  return (
    <section className="card">
      <header className="card-head">
        <span className={`step-badge${done ? ' step-done' : ''}`}>{done ? '✓' : index}</span>
        <h2>{title}</h2>
      </header>
      <div className="card-body">{children}</div>
    </section>
  );
}

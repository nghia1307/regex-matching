interface Props {
  title?: string;
  message: string;
  onDismiss?: () => void;
}

export function ErrorBanner({ title, message, onDismiss }: Props) {
  return (
    <div className="banner banner-error" role="alert">
      <div>
        {title && <strong>{title}: </strong>}
        <span>{message}</span>
      </div>
      {onDismiss && (
        <button type="button" className="btn-icon" onClick={onDismiss} aria-label="Dismiss">
          x
        </button>
      )}
    </div>
  );
}

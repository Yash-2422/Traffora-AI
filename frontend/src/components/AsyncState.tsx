import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import type { ApiError } from "../lib/api";

export function LoadingBlock({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-slate-400">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

export function EmptyBlock({
  title,
  description,
  icon: Icon = Inbox,
  action,
}: {
  title: string;
  description?: string;
  icon?: typeof Inbox;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center">
      <Icon className="h-6 w-6 text-slate-600" aria-hidden />
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {description && <p className="max-w-md text-xs leading-relaxed text-slate-500">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorBlock({
  error,
  onRetry,
  compact = false,
}: {
  error: ApiError;
  onRetry?: () => void;
  compact?: boolean;
}) {
  return (
    <div
      role="alert"
      className={`flex flex-col gap-2 rounded-lg border border-rose-500/25 bg-rose-500/[0.07] ${
        compact ? "p-3" : "p-4"
      }`}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-400" aria-hidden />
        <div className="min-w-0">
          <p className="text-sm font-medium text-rose-200">{error.message}</p>
          <p className="mt-0.5 text-[11px] text-rose-300/70">
            {error.code ? `code: ${error.code}` : "request failed"}
            {error.status ? ` · HTTP ${error.status}` : ""}
          </p>
        </div>
      </div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="btn-ghost w-fit px-2.5 py-1 text-xs">
          <RefreshCw className="h-3 w-3" aria-hidden />
          Retry
        </button>
      )}
    </div>
  );
}

/** Standard wrapper: loading -> error -> empty -> content. */
export function AsyncSection({
  loading,
  error,
  isEmpty,
  emptyTitle,
  emptyDescription,
  onRetry,
  children,
}: {
  loading: boolean;
  error: ApiError | null;
  isEmpty?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  onRetry?: () => void;
  children: ReactNode;
}) {
  if (error) return <ErrorBlock error={error} onRetry={onRetry} />;
  if (loading) return <LoadingBlock />;
  if (isEmpty) return <EmptyBlock title={emptyTitle ?? "Nothing to show"} description={emptyDescription} />;
  return <>{children}</>;
}

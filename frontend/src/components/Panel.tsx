import type { ReactNode } from "react";

interface PanelProps {
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Remove body padding (for tables and maps that go edge to edge). */
  flush?: boolean;
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = "",
  bodyClassName = "",
  flush = false,
}: PanelProps) {
  return (
    <section className={`panel flex min-w-0 flex-col ${className}`}>
      {(title || actions) && (
        <header className="panel-header">
          <div className="min-w-0">
            {title && <h2 className="panel-title truncate">{title}</h2>}
            {subtitle && <p className="panel-subtitle mt-0.5">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={`${flush ? "" : "p-4"} min-w-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

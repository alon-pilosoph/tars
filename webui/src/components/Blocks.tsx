import type { ReactNode } from "react";

export function Group({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <section className="group">
      <h2 className="group-head">
        <b>{title}</b>
        {count}
      </h2>
      {children}
    </section>
  );
}

export function Empty({
  title,
  text,
  quip,
  slabs,
  className,
  role,
  children,
}: {
  title: string;
  text: ReactNode;
  quip?: string;
  slabs?: boolean;
  className?: string;
  role?: "alert";
  children?: ReactNode;
}) {
  return (
    <div className={className ? `empty ${className}` : "empty"} role={role}>
      {slabs && (
        <div className="slabs">
          <i />
          <i />
          <i />
        </div>
      )}
      <h2>{title}</h2>
      <p>{text}</p>
      {children}
      {quip && <div className="quip">{quip}</div>}
    </div>
  );
}

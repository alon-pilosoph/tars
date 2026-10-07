import type { ReactNode } from "react";

export function Empty({
  title,
  text,
  role,
  children,
}: {
  title: string;
  text: ReactNode;
  role?: "alert";
  children?: ReactNode;
}) {
  return (
    <div className="empty" role={role}>
      <h2>{title}</h2>
      <p>{text}</p>
      {children}
    </div>
  );
}

export function SecHead({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="sec-head">
      <h2 className="sec-title">{title}</h2>
      {children}
    </div>
  );
}

export function PageHead({ title, sub, children }: { title: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {children}
    </div>
  );
}

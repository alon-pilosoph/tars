import { retry, useStore } from "../store";
import { Empty } from "./Blocks";

interface SkOptions {
  className?: string;
  marginTop?: number;
}

function sk(height: number, width: number | string, { className = "", marginTop }: SkOptions = {}) {
  return <div className={className ? `sk ${className}` : "sk"} style={{ height, width, marginTop }} />;
}

export function Loading() {
  // The same rows as a conversation (see Conversation.tsx), so the page doesn't jump when it loads.
  const conv = (key: number) => (
    <div className="conv" key={key}>
      <div className="conv-head">
        {sk(14, 64)}
        {sk(14, 48)}
      </div>
      <div className="turns">
        <div className="turn person">
          <div className="turn-who">{sk(10, 40)}</div>
          <div className="turn-line sk-line">
            <div className="sk circle" />
            {sk(14, "60%")}
          </div>
        </div>
        <div className="turn tars">
          <div className="turn-who">{sk(10, 40)}</div>
          <div className="turn-body">{sk(14, "80%", { className: "sk-text" })}</div>
        </div>
      </div>
    </div>
  );
  return (
    <>
      <div className="head">
        <div>
          {sk(28, 220)}
          {sk(14, 300, { marginTop: 12 })}
        </div>
      </div>
      <div className="conv-list" aria-busy="true" aria-label="Loading">
        {[0, 1, 2, 3].map(conv)}
      </div>
    </>
  );
}

export function Unreachable() {
  const { error } = useStore();
  return (
    <Empty
      slabs
      className="error"
      role="alert"
      title="Can't reach TARS."
      text="The page couldn't reach the assistant. Check that TARS is running, then try again."
    >
      {error && <p className="error-reason">{error}</p>}
      <button className="btn" onClick={retry}>
        Try again
      </button>
    </Empty>
  );
}

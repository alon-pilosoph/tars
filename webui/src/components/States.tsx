import { retry, useStore } from "../store";
import { Slabs } from "./Slabs";

const sk = (height: number, width: number | string, marginTop?: number) => (
  <div className="sk" style={{ height, width, marginTop }} />
);

export function Loading() {
  const conv = (i: number) => (
    <div className="conv" key={i}>
      <div className="conv-h">
        {sk(14, 64)}
        {sk(14, 48)}
      </div>
      <div className="turns">
        <div className="turn">
          {sk(10, 40, 9)}
          <div className="tl">
            <div className="sk" style={{ width: 32, height: 32, borderRadius: "50%" }} />
            {sk(16, "60%", 8)}
          </div>
        </div>
        <div className="turn">
          {sk(10, 40, 9)}
          {sk(14, "80%", 6)}
        </div>
      </div>
    </div>
  );
  return (
    <>
      <div className="head">
        <div>
          {sk(28, 220)}
          {sk(14, 300, 12)}
        </div>
      </div>
      <div className="convs" aria-busy="true" aria-label="Loading">
        {[0, 1, 2, 3].map(conv)}
      </div>
    </>
  );
}

export function Unreachable() {
  const { error } = useStore();
  return (
    <div className="empty err" role="alert">
      <Slabs />
      <h2>Can't reach TARS.</h2>
      <p>
        The page couldn't talk to the assistant ({error}). Is <code>voice-assistant --web</code> still running?
      </p>
      <button className="btn" onClick={retry}>
        Try again
      </button>
    </div>
  );
}

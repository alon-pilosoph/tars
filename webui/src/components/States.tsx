import { retry, useStore } from "../store";
import { Empty } from "./Blocks";

export function Loading() {
  return (
    <div className="skel" aria-busy="true" aria-label="Loading">
      <span className="sr">Loading</span>
      <div>
        <i className="h" />
        <i style={{ width: "46%" }} />
      </div>
      {[0, 1, 2].map(n => (
        <div className="skel-row" key={n}>
          <i style={{ width: 40 }} />
          <div>
            <i style={{ width: "82%" }} />
            <i style={{ width: "58%" }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Unreachable() {
  const { error } = useStore();
  return (
    <Empty
      role="alert"
      title="Can't reach TARS"
      text="The Pi didn't answer. It may be restarting, or this phone isn't on the home network."
    >
      {error && <p className="hint">{error}</p>}
      <div className="btns">
        <button className="btn primary big" onClick={retry}>
          Try again
        </button>
      </div>
    </Empty>
  );
}

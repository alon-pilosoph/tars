import { metricChange, stamp } from "../format";
import { linkTo } from "../params";
import { rollback, setTab, useStore } from "../store";
import type { Metric } from "../types";
import { PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";

const fileName = (path: string | null | undefined) => path?.split("/").pop() || "";

function change(m: Metric) {
  const c = metricChange(m);
  if (!c) return { text: "", verdict: "same" as const };
  if (c.verdict === "same") return { text: "Same", verdict: c.verdict };
  const unit = String(m.current).includes("%") ? " pts" : "";
  const size = Math.abs(Math.round(c.diff * 10) / 10);
  return { text: `${c.diff > 0 ? "+" : "−"}${size}${unit}, ${c.verdict}`, verdict: c.verdict };
}

export function Models() {
  const s = useStore();
  const m = s.models;
  const a = m?.active ?? null;
  const results = m?.results || [];
  const since = a ? a.version : "the version in use";
  const L = m?.learning;
  return (
    <>
      <PageHead title="Models" sub="The wake word TARS listens for, and how each version tested." />
      <SecHead title="In use" />
      {a ? (
        <dl className="kv">
          <dt>Version</dt>
          <dd>{`${a.version}${a.replaced ? `, replaced ${a.replaced}` : ""}`}</dd>
          <dt>Wake model</dt>
          <dd>
            {fileName(a.wake_model)}
            <span className="path">{a.wake_model}</span>
          </dd>
          <dt>Wake threshold</dt>
          <dd>{a.threshold}</dd>
          <dt>Double-check</dt>
          <dd>
            {a.check_model ? (
              <>
                {fileName(a.check_model)}
                <span className="path">{a.check_model}</span>
              </>
            ) : (
              "None, a plain phrase match"
            )}
          </dd>
          <dt>Check window</dt>
          <dd>{`${a.check_window_s.toFixed(1)} s`}</dd>
        </dl>
      ) : (
        <p className="muted">TARS didn't say which models it's listening with.</p>
      )}
      {results.length > 0 && a?.replaced ? (
        <>
          <SecHead title={`How ${a.version} compares with ${a.replaced}`} />
          <table className="cmp">
            <thead>
              <tr>
                <th>Test</th>
                <th>{a.replaced}</th>
                <th>{a.version}</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>
              {results.map(r => {
                const c = change(r);
                return (
                  <tr key={r.name}>
                    <td>{r.name}</td>
                    <td>{r.current}</td>
                    <td>{r.candidate}</td>
                    <td className={c.verdict}>{c.text}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      ) : (
        <p className="hint models-note">
          Nothing trained yet. TARS is using the models it was installed with. Answer some wakes in Review, then train a
          version from them.
        </p>
      )}
      {L && (
        <>
          <SecHead title="For the next training">
            <span className="muted">Collected since {since}</span>
          </SecHead>
          <div className="counts">
            <div>
              <b>{L.real}</b>
              <span>real hey TARS</span>
            </div>
            <div>
              <b>{L.missed}</b>
              <span>missed ones</span>
            </div>
            <div>
              <b>{L.not_real}</b>
              <span>not for TARS</span>
            </div>
          </div>
          {L.to_review > 0 && (
            <p className="hint models-note">
              {`${L.to_review} more are waiting in `}
              <a
                href={linkTo({ tab: "review" })}
                onClick={e => {
                  if (e.metaKey || e.ctrlKey || e.shiftKey) return;
                  e.preventDefault();
                  setTab("review");
                }}
              >
                Review
              </a>
              .
            </p>
          )}
        </>
      )}
      <SecHead title="History" />
      {m?.problem && <p className="hint warn">The saved history is being ignored: {m.problem}</p>}
      <div className="history">
        {(m?.history || []).map(h => (
          <div key={h.version} className="ver">
            <div className="ver-name">{h.version}</div>
            <div>
              <div>{h.note}</div>
              <div className="ver-when">{stamp(h.ts)}</div>
            </div>
            {h.active ? (
              <span className="in-use">In use</span>
            ) : (
              <button className="btn" onClick={() => rollback(h.version)}>
                Use this
              </button>
            )}
          </div>
        ))}
      </div>
      <details className="how">
        <summary>
          <Icon name="chev" size="s" />
          How to train
        </summary>
        <div className="inner">
          <p className="muted">Training runs on a bigger machine, in the TARS folder:</p>
          <pre className="cmd">uv run --group training python -m training.household</pre>
          <p className="hint">
            It learns from the wakes you've answered since {since}, tests the new pair against the one in use, and
            installs it only if it's better. More in training/README.md.
          </p>
        </div>
      </details>
    </>
  );
}

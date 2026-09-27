import { stamp } from "../format";
import { rollback, setTab, useStore } from "../store";
import type { Metric, Models as ModelsInfo } from "../types";

export function Models() {
  const s = useStore();
  const m = s.models,
    a = m?.active || {},
    h = m?.history || [],
    rows = m?.results || [];
  return (
    <>
      <div className="head">
        <div>
          <h1>Models</h1>
          <p>What TARS uses to hear its name, and what it can learn from next.</p>
        </div>
      </div>
      <div className="panel">
        <h2>In use now{a.version && a.version !== "installed" ? `: ${a.version}` : ""}</h2>
        <dl className="spec">
          <div>
            <dt>Wake model</dt>
            <dd>{a.wake_model || "—"}</dd>
          </div>
          <div>
            <dt>Wake threshold</dt>
            <dd>{a.threshold ?? "—"}</dd>
          </div>
          <div>
            <dt>Double-check</dt>
            <dd>{a.check_model || "—"}</dd>
          </div>
          <div>
            <dt>Check window</dt>
            <dd>{a.check_window_s ? `${a.check_window_s} s` : "—"}</dd>
          </div>
        </dl>
        {rows.length > 0 && (
          <div className="result">
            <p className="m">How it tested against the models it replaced:</p>
            <table>
              <thead>
                <tr>
                  <th>Test</th>
                  <th className="num">Before</th>
                  <th className="num">These</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.name}>
                    <td>{r.name}</td>
                    <td className="num">{r.current}</td>
                    <td className="num">
                      {r.candidate}
                      <Delta m={r} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="panel">
        <h2>Waiting to learn from</h2>
        <Learning m={m} />
        <p>
          Training runs on a bigger machine, not here (see <code className="mono">training/README.md</code>): it learns
          from these, tests the new models against the ones in use, and installs them only if they're better.
        </p>
      </div>
      <div className="panel">
        <h2>History</h2>
        {m?.problem && <div className="notice">{m.problem}</div>}
        {h.length ? (
          <div className="hist" role="table">
            <div className="h">Version</div>
            <div className="h">When</div>
            <div className="h">What changed</div>
            <div className="h" />
            {h.map((x, i) => {
              const last = i === h.length - 1 ? " last" : "";
              return [
                <div key={`v${i}`} className={`v${last}`}>
                  {x.version}
                  {x.active && <span className="tag">in use</span>}
                </div>,
                <div key={`w${i}`} className={`w${last}`}>
                  {stamp(x.ts)}
                </div>,
                <div key={`n${i}`} className={`note${last}`}>
                  {x.note}
                </div>,
                <div key={`a${i}`} className={`a${last}`}>
                  {!x.active && (
                    <button className="btn sm" onClick={() => rollback(x.version)}>
                      Use this
                    </button>
                  )}
                </div>,
              ];
            })}
          </div>
        ) : (
          <p>Only the models TARS was installed with, so far.</p>
        )}
      </div>
    </>
  );
}

function Learning({ m }: { m: ModelsInfo | null }) {
  const l = m?.learning,
    since = m?.active.version && m.active.version !== "installed" ? ` since ${m.active.version}` : "";
  if (!l) return null;
  const learned = [
    [l.real, "real hey TARS"],
    [l.missed, "missed hey TARS"],
    [l.not_real, "not for TARS"],
  ] as const;
  return (
    <>
      <dl className="spec">
        {learned.map(([n, what]) => (
          <div key={what}>
            <dt>{what}</dt>
            <dd>{n}</dd>
          </div>
        ))}
        <div>
          <dt>to answer in Review</dt>
          <dd>{l.to_review}</dd>
        </div>
      </dl>
      <p>
        Labeled wakes{since}. A missed "hey TARS" is a near-miss followed by a real wake: the ones the wake model most
        needs to learn from.
      </p>
      {l.to_review > 0 && (
        <div className="train-row">
          <button className="btn sm" onClick={() => setTab("review")}>
            Answer them in Review
          </button>
        </div>
      )}
    </>
  );
}

/** Signed change from before to after, green when it's better. */
function Delta({ m }: { m: Metric }) {
  const n = (v: string) => parseFloat(v) || 0;
  const cur = m.current,
    cand = m.candidate;
  if (isNaN(parseFloat(cur)) || isNaN(parseFloat(cand))) return null;
  const lower = !!m.lower_is_better,
    d = n(cand) - n(cur);
  if (!d) return <span className="delta">same</span>;
  const good = lower ? d < 0 : d > 0,
    pct = String(cur).includes("%");
  return (
    <span className={`delta ${good ? "up" : "down"}`}>
      {d > 0 ? "+" : "−"}
      {Math.abs(Math.round(d * 10) / 10)}
      {pct ? " pts" : ""}
    </span>
  );
}

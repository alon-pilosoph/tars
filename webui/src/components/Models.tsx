import { plural, stamp, when } from "../format";
import { retrain, rollback, useStore } from "../store";
import type { Metric, RetrainResult } from "../types";

export function Models() {
  const s = useStore();
  const a = s.models?.active || {},
    h = s.models?.history || [],
    labeled = s.events.filter(e => e.label || e.auto_label).length;
  return (
    <>
      <div className="head">
        <div>
          <h1>Models</h1>
          <p>What TARS uses to hear its name. It only retrains when you ask.</p>
        </div>
      </div>
      <div className="panel">
        <h2>In use now</h2>
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
      </div>
      <div className="panel">
        <h2>Retrain the double-check</h2>
        <p>
          Trains on your {plural(labeled, "labeled event")} (conversations label themselves), then tests the candidate
          on a fixed set: your held-out recordings, held-out voices, an hour of TV and an hour of audiobooks. It only
          replaces the current check if it does better.
        </p>
        <div className="train-row">
          <button className="btn primary" disabled={s.retraining} onClick={retrain}>
            {s.retraining ? (
              <>
                <span className="spin" />
                Retraining…
              </>
            ) : (
              "Retrain now"
            )}
          </button>
          <span className="note">{retrainNote(s.retraining, labeled)}</span>
        </div>
        <Result r={s.lastRetrain} retraining={s.retraining} />
      </div>
      <div className="panel">
        <h2>History</h2>
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
          <p>Only the model you started with so far.</p>
        )}
      </div>
    </>
  );
}

/** Signed change from current to candidate, green when it's better. "0 / 0" (TV / audiobooks) counts as the sum. */
function Delta({ m }: { m: Metric }) {
  const n = (v: string) =>
    String(v)
      .split("/")
      .reduce((a, x) => a + (parseFloat(x) || 0), 0);
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

function retrainNote(retraining: boolean, labeled: number) {
  if (retraining) return "This takes a few minutes.";
  return labeled ? "" : "Talk to TARS for a few days first: conversations are what it learns from.";
}

const SPIN = {
  width: 12,
  height: 12,
  border: "2px solid currentColor",
  borderRightColor: "transparent",
  borderRadius: "50%",
  animation: "spin .8s linear infinite",
};

function Result({ r, retraining }: { r: RetrainResult | null | undefined; retraining: boolean }) {
  if (retraining)
    return (
      <div className="result">
        <div className="verdict">
          <span className="badge mute">
            <span className="spin" style={SPIN} />
            Retraining
          </span>
          <span className="m">building a candidate and testing it against the fixed set</span>
        </div>
        <div className="progress">
          <i />
        </div>
      </div>
    );
  if (!r)
    return (
      <div className="result">
        <p style={{ margin: 0, color: "var(--ink-3)" }}>No retrain yet.</p>
      </div>
    );
  if (r.status === "not_built")
    return (
      <div className="result">
        <div className="notice">{r.summary}</div>
      </div>
    );
  const rows = r.metrics || [];
  return (
    <div className="result">
      <div className="verdict">
        {r.status === "swapped" ? (
          <>
            <span className="badge yes">✓ Better: now in use</span>
            <span className="mono" style={{ fontWeight: 500 }}>
              {r.version || ""}
            </span>
          </>
        ) : (
          <span className="badge no">✗ Not better: kept the current check</span>
        )}
        <span className="m">
          Trained on {plural(r.labeled ?? 0, "labeled event")}
          {r.ts ? `, ${when(r.ts)}` : ""}
        </span>
      </div>
      {rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Test</th>
              <th className="num">Current</th>
              <th className="num">Candidate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(m => (
              <tr key={m.name}>
                <td>{m.name}</td>
                <td className="num">{m.current}</td>
                <td className="num">
                  {m.candidate}
                  <Delta m={m} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

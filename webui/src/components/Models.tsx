import { INSTALLED, type Verdict, metricChange, plural, sentence, shortStamp, versionName, when } from "../format";
import { rollback, setTab, useStore } from "../store";
import type { Metric, ModelsInfo, Version } from "../types";

const fileName = (path: string | null | undefined) => path?.split("/").pop() || "";

export function Models() {
  const s = useStore();
  const models = s.models;
  const active = models?.active ?? null;
  const history = models?.history || [];
  const results = models?.results || [];
  const inUse = history.find(v => v.active);
  const version = active?.version || INSTALLED;
  const files = [fileName(active?.wake_model), fileName(active?.check_model)].filter(Boolean).join(", ");
  const checkModel = !active ? "—" : (active.check_model ?? "None: TARS listens for the phrase itself");
  return (
    <>
      <div className="head">
        <div>
          <h1>Models</h1>
          <p>{status(version, inUse, results)}</p>
        </div>
      </div>
      <div className="models-grid">
        <section className="panel">
          <h2>In use</h2>
          <div className="version">{version}</div>
          {inUse?.note ? <p>{inUse.note}</p> : null}
          <dl className="pairs">
            <div>
              <dt>Wake threshold</dt>
              <dd>{active?.threshold ?? "—"}</dd>
            </div>
            <div>
              <dt>Check window</dt>
              <dd>{active?.check_window_s ? `${active.check_window_s} s` : "—"}</dd>
            </div>
          </dl>
          <details className="files">
            <summary>Files: {files || "—"}</summary>
            <dl>
              <dt>Wake model</dt>
              <dd>{active?.wake_model || "—"}</dd>
              <dt>Double-check</dt>
              <dd>{checkModel}</dd>
            </dl>
          </details>
        </section>
        <Learning models={models} />
      </div>
      {results.length > 0 && <Compare rows={results} before={active?.replaced || null} after={version} />}
      <section className="panel">
        <h2>History</h2>
        {models?.problem && <div className="notice">{models.problem}</div>}
        {history.length ? (
          <ol className="hist">
            {history.map(v => (
              <li key={v.version}>
                <div className="hist-version">
                  <b>{v.version}</b>
                  <span>{shortStamp(v.ts)}</span>
                </div>
                <p>{v.note}</p>
                <div className="hist-action">
                  {v.active ? (
                    <span className="in-use">In use</span>
                  ) : (
                    <button className="btn sm" onClick={() => rollback(v.version)}>
                      Use this…
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p>Only the models TARS was installed with, so far.</p>
        )}
      </section>
      <p className="models-foot">Training runs on another machine and installs new models only if they test better.</p>
      <details className="howto">
        <summary>How to train</summary>
        <pre>
          <code>uv run --group training python -m training.household</code>
        </pre>
        <p>
          It learns from the wakes you've answered, tests the new models against the ones in use, and installs them only
          if they're better. More in training/README.md.
        </p>
      </details>
    </>
  );
}

function status(version: string, inUse: Version | undefined, rows: Metric[]) {
  if (version === INSTALLED) return "Using the models TARS was installed with.";
  const verdicts = rows.map(r => metricChange(r)?.verdict);
  const count = (v: Verdict) => verdicts.filter(x => x === v).length;
  const parts = [
    count("better") && `better on ${plural(count("better"), "test")}`,
    count("worse") && `worse on ${plural(count("worse"), "test")}`,
    count("same") && `the same on ${count("same")}`,
  ].filter(Boolean);
  const tested = parts.length ? ` ${sentence(parts.join(", "))}` : "";
  return `Using ${version}${inUse ? `, trained ${when(inUse.ts)}` : ""}.${tested}`;
}

function Learning({ models }: { models: ModelsInfo | null }) {
  const learning = models?.learning;
  if (!learning) return null;
  const total = learning.real + learning.missed + learning.not_real;
  return (
    <section className="panel">
      <h2>For the next training</h2>
      {total ? (
        <>
          <dl className="stats">
            <div>
              <dt>Real “hey TARS”</dt>
              <dd>{learning.real}</dd>
            </div>
            <div>
              <dt>Missed “hey TARS”</dt>
              <dd>{learning.missed}</dd>
            </div>
            <div>
              <dt>Not for TARS</dt>
              <dd>{learning.not_real}</dd>
            </div>
          </dl>
          <p className="help">
            A missed one is a near-miss followed by a real wake. The wake model learns the most from these.
          </p>
        </>
      ) : (
        <p>Nothing new yet. Wakes you answer in Review collect here.</p>
      )}
      {learning.to_review > 0 && (
        <button className="btn sm" onClick={() => setTab("review")}>
          Answer {plural(learning.to_review, "wake")} in Review
        </button>
      )}
    </section>
  );
}

function Compare({ rows, before, after }: { rows: Metric[]; before: string | null; after: string }) {
  const name = (v: string) => <span className="version-name">{v}</span>;
  const beforeName = !before ? "the models before it" : before === INSTALLED ? versionName(before) : name(before);
  return (
    <section className="panel">
      <h2>
        How {name(after)} compares with {beforeName}
      </h2>
      <table className="compare">
        <thead>
          <tr>
            <th>Test</th>
            <th className="num">{name(before || "Before")}</th>
            <th className="num">{name(after)}</th>
            <th className="num">Change</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.name}>
              <td>{r.name}</td>
              <td className="num">{r.current}</td>
              <td className="num after">{r.candidate}</td>
              <td className="num">
                <Delta m={r} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Delta({ m }: { m: Metric }) {
  const c = metricChange(m);
  if (!c) return null;
  if (c.verdict === "same") return <span className="delta">same</span>;
  const points = String(m.current).includes("%");
  return (
    <span className={`delta ${c.verdict === "better" ? "up" : "down"}`}>
      {c.diff > 0 ? "+" : "−"}
      {Math.abs(Math.round(c.diff * 10) / 10)}
      {points ? " pts" : ""}
    </span>
  );
}

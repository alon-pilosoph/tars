import { useEffect } from "react";
import { ago, diskLine, diskLow, duration, hot, memoryLine, pageLine, plural, processLine, time } from "../format";
import { loadHealth, useStore } from "../store";
import type { Health } from "../types";
import { PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";

const MARK = { ok: "✓", warn: "!", fail: "✗" } as const;

export function Status() {
  const s = useStore();
  useEffect(() => {
    loadHealth();
  }, []);
  const h = s.health;
  return (
    <>
      <PageHead title="Status" sub="Whether every part of TARS is running, the services it uses, and this machine." />
      {s.healthError && (
        <p className="hint warn" role="alert">
          {`Couldn't get the status (${s.healthError}). `}
          <button className="btn quiet" onClick={() => loadHealth()}>
            Try again
          </button>
        </p>
      )}
      {!h ? (
        !s.healthError && <p className="muted">Asking each part of TARS, and each service it uses…</p>
      ) : (
        <Body h={h} />
      )}
    </>
  );
}

function Body({ h }: { h: Health }) {
  const m = h.machine;
  const t = h.today;
  return (
    <>
      <SecHead title="TARS" />
      <table className="cmp health">
        <tbody>
          {h.processes.map(p => {
            const line = processLine(p, h.now);
            return (
              <tr key={p.name}>
                <td>{p.label}</td>
                <td className={line.bad ? "bad" : undefined}>{line.text}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="counts health-counts">
        <div>
          <b>{t.wakes}</b>
          <span>{t.wakes === 1 ? "wake today" : "wakes today"}</span>
        </div>
        <div>
          <b>{t.answers}</b>
          <span>{t.answers === 1 ? "answer" : "answers"}</span>
        </div>
        <div className={t.failed ? "bad" : undefined}>
          <b>{t.failed}</b>
          <span>{t.failed === 1 ? "answer failed" : "answers failed"}</span>
        </div>
      </div>
      <p className="hint">{t.last_wake != null ? `The last wake was ${ago(t.last_wake, h.now)}.` : "No wakes yet."}</p>

      <SecHead title="Services">
        <span className="muted">{`Asked at ${time(h.services_asked)}`}</span>
      </SecHead>
      {h.services.length === 0 ? (
        <p className="muted">TARS uses no cloud service with the keys it has.</p>
      ) : (
        <table className="cmp health">
          <thead>
            <tr>
              <th>Service</th>
              <th>With TARS's key</th>
              <th>Its own status page</th>
            </tr>
          </thead>
          <tbody>
            {h.services.map(svc => {
              const page = pageLine(svc.page);
              return (
                <tr key={svc.name}>
                  <td>{svc.name}</td>
                  <td className={svc.status === "ok" ? undefined : "bad"}>
                    <span aria-hidden="true">{MARK[svc.status]} </span>
                    {svc.detail}
                    {svc.fix && svc.status !== "ok" && <span className="fix">{svc.fix}</span>}
                  </td>
                  <td className={page.bad ? "bad" : undefined}>
                    {svc.page ? (
                      <a href={svc.page.url} target="_blank" rel="noreferrer">
                        {page.text}
                        <Icon name="open" size="s" />
                      </a>
                    ) : (
                      <span className="muted">{page.text}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <SecHead title="This machine" />
      <dl className="kv">
        <dt>Name</dt>
        <dd>{m.host}</dd>
        <dt>System</dt>
        <dd>{`${m.system}, Python ${m.python}`}</dd>
        <dt>Processor</dt>
        <dd>{`${Math.round(m.cpu_percent)}% busy; load ${m.load.join(", ")} on ${plural(m.cores, "core")}`}</dd>
        <dt>Memory</dt>
        <dd>{memoryLine(m)}</dd>
        <dt>Temperature</dt>
        <dd className={hot(m) ? "bad" : undefined}>
          {m.temperature_c != null ? `${m.temperature_c.toFixed(1)} °C` : "Not reported here"}
        </dd>
        {m.throttled && (
          <>
            <dt>Power and heat</dt>
            <dd className={m.throttled.length ? "bad" : undefined}>
              {m.throttled.length ? m.throttled.map(t => <div key={t}>{t}</div>) : "Fine"}
            </dd>
          </>
        )}
        <dt>Storage</dt>
        <dd className={diskLow(m) ? "bad" : undefined}>{diskLine(m)}</dd>
        <dt>Up for</dt>
        <dd>{duration(m.uptime_s)}</dd>
      </dl>
    </>
  );
}

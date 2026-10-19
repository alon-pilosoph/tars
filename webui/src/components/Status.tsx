import { useEffect } from "react";
import {
  ago,
  diskLine,
  duration,
  memoryLine,
  pageLine,
  processLine,
  processorLine,
  temperatureLine,
  time,
} from "../format";
import { loadHealth, useStore } from "../store";
import type { Health } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";
import { Loading } from "./States";

const MARK = { ok: "✓", warn: "!", fail: "✗" } as const; // check.py's

export function StatusView() {
  const s = useStore();
  useEffect(() => {
    loadHealth();
  }, []);
  return (
    <>
      <PageHead title="Status" sub="Whether every part of TARS is running, the services it uses, and this machine." />
      {s.health ? (
        <Body h={s.health} />
      ) : s.healthError ? (
        <Empty role="alert" title="Couldn't get the status" text={s.healthError}>
          <div className="btns">
            <button className="btn primary big" onClick={() => loadHealth()}>
              Try again
            </button>
          </div>
        </Empty>
      ) : (
        <Loading />
      )}
    </>
  );
}

function Count({ n, one, many, bad }: { n: number; one: string; many: string; bad?: boolean }) {
  return (
    <div className={bad ? "bad" : undefined}>
      <b>{n}</b>
      <span>{n === 1 ? one : many}</span>
    </div>
  );
}

function Body({ h }: { h: Health }) {
  const { machine: m, today } = h;
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
        <Count n={today.wakes} one="wake today" many="wakes today" />
        <Count n={today.answers} one="answer" many="answers" />
        <Count n={today.failed} one="answer failed" many="answers failed" bad={today.failed > 0} />
      </div>
      <p className="hint">
        {today.last_wake != null ? `The last wake was ${ago(today.last_wake, h.now)}.` : "No wakes yet."}
      </p>

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
        <dd>{processorLine(m)}</dd>
        <dt>Memory</dt>
        <dd>{memoryLine(m)}</dd>
        <dt>Temperature</dt>
        <dd className={m.hot ? "bad" : undefined}>{temperatureLine(m)}</dd>
        {m.throttled && (
          <>
            <dt>Power and heat</dt>
            <dd className={m.throttled.length ? "bad" : undefined}>
              {m.throttled.length ? m.throttled.map(said => <div key={said}>{said}</div>) : "Fine"}
            </dd>
          </>
        )}
        <dt>Storage</dt>
        <dd className={m.disk_low ? "bad" : undefined}>{diskLine(m)}</dd>
        <dt>Up for</dt>
        <dd>{duration(m.uptime_s)}</dd>
      </dl>
    </>
  );
}

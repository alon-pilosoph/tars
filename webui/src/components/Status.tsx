import { type ReactNode, useEffect } from "react";
import {
  ago,
  checkLine,
  diskLine,
  duration,
  memoryLine,
  pageLine,
  processLine,
  processorLine,
  temperatureLine,
  time,
  dollars,
  personName,
  serviceModel,
  usageLine,
} from "../format";
import { loadHealth, useStore } from "../store";
import type { CheckLine, Health, MonthUsage, UsageLine } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";
import { LoadError, Loading } from "./States";

export function StatusView() {
  const s = useStore();
  useEffect(() => {
    loadHealth();
  }, []);
  return (
    <>
      <PageHead
        title="Status"
        sub={`Whether every part of TARS is running, the services it uses, ${s.health?.backup ? "its backup, " : ""}and this machine.`}
      />
      {s.health ? (
        <>
          {s.healthError && <LoadError what="the status" why={s.healthError} />}
          <Body h={s.health} />
        </>
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

function CheckCell({ line }: { line: CheckLine }) {
  const c = checkLine(line);
  return (
    <td className={c.bad ? "bad" : undefined}>
      <span aria-hidden="true">{c.mark} </span>
      {c.text}
      {c.fix && <span className="fix">{c.fix}</span>}
    </td>
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
      {today.last_wake != null && <p className="hint">{`The last wake was ${ago(today.last_wake, h.now)}.`}</p>}

      <SecHead title="Services">
        <span className="muted">{`Asked at ${time(h.services_asked)}`}</span>
      </SecHead>
      {h.services.length === 0 ? (
        <p className="muted">TARS uses no cloud service with the keys it has.</p>
      ) : (
        <table className="cmp health services">
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
                  <CheckCell line={svc} />
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

      {h.usage && <Usage u={h.usage} />}

      {h.backup && (
        <>
          <SecHead title="Backup" />
          <table className="cmp health backup">
            <tbody>
              {h.backup.map(b => (
                <tr key={b.name}>
                  <td>{b.name}</td>
                  <CheckCell line={b} />
                </tr>
              ))}
            </tbody>
          </table>
        </>
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

function Usage({ u }: { u: MonthUsage }) {
  const line = usageLine(u);
  return (
    <>
      <SecHead title="Paid services this month">
        <span className="muted">{`Prices checked ${u.checked}`}</span>
      </SecHead>
      <p className={line.bad ? "usage-total bad" : "usage-total"}>{line.text}</p>
      {u.by_service.length === 0 ? (
        <p className="muted">Nothing used yet this month.</p>
      ) : (
        <>
          <UsageTable what="Service" lines={u.by_service} name={serviceCell} />
          <UsageTable what="Person" lines={u.by_person} name={personCell} />
        </>
      )}
      {u.unpriced > 0 && (
        <p className="hint">{`${u.unpriced} ${u.unpriced === 1 ? "call" : "calls"} to something with no price in prices.toml aren't in the cost.`}</p>
      )}
      {u.unreported > 0 && (
        <p className="hint">{`${u.unreported} ${u.unreported === 1 ? "call was" : "calls were"} cut off before the provider said what it used, so ${u.unreported === 1 ? "it's" : "they're"} counted as nothing.`}</p>
      )}
      <p className="hint">Estimates from TARS's own count, not a bill: the providers' dashboards are.</p>
    </>
  );
}

function serviceCell(name: string) {
  const { service, model } = serviceModel(name);
  return (
    <>
      {service}
      <span className="model">{model}</span>
    </>
  );
}

function personCell(name: string) {
  return name ? personName(name) : <span className="muted">Nobody known</span>;
}

function UsageTable({ what, lines, name }: { what: string; lines: UsageLine[]; name: (name: string) => ReactNode }) {
  return (
    <table className="cmp health usage">
      <thead>
        <tr>
          <th>{what}</th>
          <th>Calls</th>
          <th>Cost</th>
        </tr>
      </thead>
      <tbody>
        {lines.map(l => (
          <tr key={l.name}>
            <td>{name(l.name)}</td>
            <td>{l.calls.toLocaleString("en")}</td>
            <td>
              {dollars(l.cost)}
              {l.unpriced > 0 && <span className="model">and some not priced</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

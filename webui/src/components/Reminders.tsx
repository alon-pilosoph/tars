import { type FormEvent, useState } from "react";
import { DEMO_LINK } from "../demoParams";
import { personName, reminderEnded, reminderFor, reminderNow, reminderWhen, stamp } from "../format";
import { DEMO, linkTo } from "../params";
import { ackReminder, cancelReminder, convById, goConv, setReminder, snoozeReminder, useStore } from "../store";
import type { Reminder, ReminderKind, RemindersInfo } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";

const SNOOZE_MIN = 10;
const SUB = "Timers, reminders and messages TARS says aloud, and says again until someone says “got it”.";

export function Reminders() {
  const s = useStore();
  const info = s.reminders;
  const [adding, setAdding] = useState(() => DEMO && !!DEMO_LINK.form);
  if (!info?.enabled)
    return (
      <>
        <PageHead title="Reminders" sub={SUB} />
        <Empty
          title="Reminders are off"
          text="TARS isn't saying reminders aloud, and won't take new ones. Turn reminders on in TARS's settings on the Pi, then restart TARS and press Refresh."
        />
      </>
    );
  const people = [
    ...new Set([
      ...info.voices.map(personName),
      ...s.clusters.filter(c => c.kind === "person" && c.name).map(c => personName(c.name!)),
    ]),
  ].sort();
  const rs = info.reminders;
  const now = rs.filter(r => r.status === "waiting");
  const next = rs.filter(r => r.status === "scheduled");
  const past = rs.filter(r => r.status !== "waiting" && r.status !== "scheduled");
  return (
    <>
      <PageHead title="Reminders" sub={SUB}>
        {!adding && (
          <button className="btn primary big" onClick={() => setAdding(true)}>
            New reminder
          </button>
        )}
      </PageHead>
      {adding && <ReminderForm info={info} people={people} onDone={() => setAdding(false)} />}
      {!rs.length && <Empty title="No reminders" text="Say “hey TARS, remind me…”, or set one here." />}
      {now.length > 0 && (
        <>
          <SecHead title="Needs someone now" />
          <div className="needs-list">
            {now.map(r => (
              <article key={r.id} className="needs" data-id={r.id}>
                <div className="status">
                  <Icon name={r.kind === "timer" ? "timer" : "message"} size="s" />
                  {reminderNow(r)}
                </div>
                <p className="says">{`“${r.says}”`}</p>
                <Meta r={r} />
                <div className="btns rem-actions">
                  {r.kind === "timer" ? (
                    <button className="btn primary big" onClick={() => ackReminder(r)}>
                      Turn off
                    </button>
                  ) : (
                    <>
                      <button className="btn primary big" onClick={() => ackReminder(r)}>
                        Got it
                      </button>
                      <button className="btn" onClick={() => snoozeReminder(r, SNOOZE_MIN)}>
                        {`Again in ${SNOOZE_MIN} min`}
                      </button>
                      <button className="btn ghost" onClick={() => cancelReminder(r)}>
                        Stop
                      </button>
                    </>
                  )}
                </div>
              </article>
            ))}
          </div>
        </>
      )}
      {next.length > 0 && (
        <>
          <SecHead title="Coming up" />
          <div className="rem-rows">
            {next.map(r => (
              <article key={r.id} className="rem" data-id={r.id}>
                <div className="rem-when">{reminderWhen(r)}</div>
                <div>
                  <p className="says">{`“${r.says}”`}</p>
                  <Meta r={r} />
                </div>
                <button className="btn ghost" onClick={() => cancelReminder(r)}>
                  Stop
                </button>
              </article>
            ))}
          </div>
        </>
      )}
      {past.length > 0 && (
        <>
          <SecHead title="Earlier" />
          <div className="rem-rows">
            {past.map(r => (
              <article key={r.id} className="rem past" data-id={r.id}>
                <div className="rem-when">{r.due != null ? stamp(r.due) : ""}</div>
                <div>
                  <p className="says">{`“${r.says}”`}</p>
                  <div className={`rem-state${r.status === "missed" ? " missed" : ""}`}>{reminderEnded(r)}</div>
                  <Meta r={r} />
                </div>
                {r.status === "missed" && r.kind !== "timer" ? (
                  <button className="btn ghost" onClick={() => snoozeReminder(r, SNOOZE_MIN)}>
                    {`Again in ${SNOOZE_MIN} min`}
                  </button>
                ) : (
                  <span />
                )}
              </article>
            ))}
          </div>
        </>
      )}
    </>
  );
}

function Meta({ r }: { r: Reminder }) {
  const s = useStore();
  const conv = r.set_via === "voice" && r.conversation_id != null && convById(s, r.conversation_id);
  return (
    <div className="r-meta">
      {conv
        ? `${reminderFor(r)}, `
        : `${reminderFor(r)}, ${r.set_via === "voice" ? "set by voice" : "set on this page"}.`}
      {conv && (
        <>
          <a
            href={linkTo({ conv: conv.id })}
            onClick={e => {
              if (e.metaKey || e.ctrlKey || e.shiftKey) return;
              e.preventDefault();
              goConv(conv.id);
            }}
          >
            set by voice
          </a>
          .
        </>
      )}
    </div>
  );
}

type When = "minutes" | "time" | "back";

interface Form {
  kind: ReminderKind;
  text: string;
  forName: string;
  forOther: string;
  fromName: string;
  when: When;
  minutes: string;
  at: string;
  needsAck: boolean;
  error: string | null;
}

const OTHER = "__other";

const local = (d: Date) => {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
function tomorrowAt(hour: number) {
  const d = new Date(Date.now() + 864e5);
  d.setHours(hour, 0, 0, 0);
  return local(d);
}

function freshForm(): Form {
  const base: Form = {
    kind: "reminder",
    text: "",
    forName: "",
    forOther: "",
    fromName: "",
    when: "minutes",
    minutes: "10",
    at: "",
    needsAck: true,
    error: null,
  };
  if (!DEMO || !DEMO_LINK.form) return base;
  const presets: Record<NonNullable<typeof DEMO_LINK.form>, Partial<Form>> = {
    minutes: { kind: "timer", text: "pasta", when: "minutes", minutes: "12" },
    time: {
      kind: "reminder",
      text: "water the plants on the balcony",
      forName: "Alon",
      when: "time",
      at: tomorrowAt(18),
    },
    back: { kind: "message", text: "the parcel is at the Cohens'", forName: "Stacey", fromName: "Alon", when: "back" },
    error: {
      kind: "message",
      text: "dinner's at eight",
      fromName: "Stacey",
      minutes: "30",
      error: "a message needs someone it's for",
    },
  };
  return { ...base, ...presets[DEMO_LINK.form] };
}

const names = (list: string[]) =>
  list.length < 2 ? list.join("") : `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`;

function ReminderForm({ info, people, onDone }: { info: RemindersInfo; people: string[]; onDone: () => void }) {
  const [f, setF] = useState(freshForm);
  const [busy, setBusy] = useState(false);
  const put = (patch: Partial<Form>) => setF(x => ({ ...x, ...patch, error: null }));
  const timer = f.kind === "timer";
  const forName = (f.forName === OTHER ? f.forOther.trim() : f.forName) || null;
  const known = !!forName && info.voices.includes(forName.toLowerCase());
  const voices = info.voices.map(personName);
  const backProblem =
    f.when !== "back"
      ? null
      : !forName
        ? "Pick who it's for, so TARS knows whose voice to wait for."
        : !known
          ? `TARS doesn't know ${personName(forName)}'s voice yet, so it can't wait for them. Pick a time instead.`
          : null;
  const problem =
    backProblem ||
    (!timer && !f.text.trim()
      ? "Say what TARS should say."
      : f.when === "minutes" && !(Number(f.minutes) > 0)
        ? "How many minutes from now?"
        : f.when === "time" && !f.at
          ? "Pick a date and time."
          : null);

  function says() {
    const who = forName ? `${personName(forName)}, ` : "";
    const text = f.text.trim();
    if (timer) {
      const label = text ? `${text} timer` : "timer";
      return who ? `${who}your ${label} is done.` : `Your ${label} is done.`;
    }
    const sender = f.fromName && f.fromName.toLowerCase() !== (forName || "").toLowerCase() ? f.fromName : null;
    const what = f.kind === "message" ? "a message" : "a reminder";
    const head = (who ? who + what : what[0].toUpperCase() + what.slice(1)) + (sender ? ` from ${sender}` : "");
    return `${head}: ${text ? (/[.!?]$/.test(text) ? text : `${text}.`) : "…"}`;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (problem) return;
    setBusy(true);
    const error = await setReminder({
      kind: f.kind,
      text: f.text.trim() || null,
      for_name: forName,
      from_name: timer ? null : f.fromName || null,
      due:
        f.when === "back"
          ? null
          : f.when === "time"
            ? new Date(f.at).getTime() / 1000
            : Date.now() / 1000 + Number(f.minutes) * 60,
      when_back: f.when === "back",
      needs_ack: timer || f.needsAck,
      repeat_every_min: null,
      max_tries: null,
    });
    setBusy(false);
    if (error) setF(x => ({ ...x, error }));
    else onDone();
  }

  const pill = <K extends "kind" | "when">(field: K, value: Form[K], label: string) => (
    <button
      type="button"
      className="btn"
      aria-pressed={f[field] === value}
      onClick={() =>
        put(
          field === "kind" && value === "timer" && f.when === "back"
            ? { kind: "timer", when: "minutes" }
            : { [field]: value },
        )
      }
    >
      {label}
    </button>
  );

  return (
    <form className="form" onSubmit={submit} noValidate aria-label="New reminder">
      <div className="form-head">
        <h2>New reminder</h2>
        <button type="button" className="icon-btn" aria-label="Close" onClick={onDone}>
          <Icon name="close" />
        </button>
      </div>
      <div className="field">
        <span className="lbl">Kind</span>
        <div className="pills">
          {pill("kind", "timer", "Timer")}
          {pill("kind", "reminder", "Reminder")}
          {pill("kind", "message", "Message")}
        </div>
      </div>
      <label className="field">
        <span className="lbl">{timer ? "Label, if you like" : "What should TARS say?"}</span>
        {timer ? (
          <input
            type="text"
            value={f.text}
            placeholder="eggs"
            maxLength={300}
            onChange={e => put({ text: e.target.value })}
          />
        ) : (
          <textarea
            rows={2}
            value={f.text}
            maxLength={300}
            placeholder={f.kind === "message" ? "the plumber is coming at four" : "call the bank about the mortgage"}
            onChange={e => put({ text: e.target.value })}
          />
        )}
      </label>
      <div className="field-row">
        <label className="field">
          <span className="lbl">For</span>
          <select value={f.forName} onChange={e => put({ forName: e.target.value })}>
            <option value="">Whoever's there</option>
            {people.map(p => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
            <option value={OTHER}>Someone else…</option>
          </select>
          {f.forName === OTHER && (
            <input
              type="text"
              value={f.forOther}
              placeholder="Their name"
              maxLength={60}
              aria-label="Their name"
              style={{ marginTop: 8 }}
              onChange={e => put({ forOther: e.target.value })}
            />
          )}
        </label>
        {!timer && (
          <label className="field">
            <span className="lbl">From, if you like</span>
            <select value={f.fromName} onChange={e => put({ fromName: e.target.value })}>
              <option value="">Nobody</option>
              {people.map(p => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="field">
        <span className="lbl">When</span>
        <div className="pills">
          {pill("when", "minutes", "In a few minutes")}
          {pill("when", "time", "At a time")}
          {!timer &&
            pill("when", "back", forName ? `When ${personName(forName)} is next heard` : "When they're next heard")}
        </div>
        {f.when === "minutes" && (
          <div className="inline">
            <input
              type="number"
              min={1}
              step="any"
              value={f.minutes}
              aria-label="Minutes from now"
              onChange={e => put({ minutes: e.target.value })}
            />
            <span className="muted">minutes from now</span>
          </div>
        )}
        {f.when === "time" && (
          <input
            type="datetime-local"
            value={f.at}
            aria-label="Date and time"
            onChange={e => put({ at: e.target.value })}
          />
        )}
        {f.when === "back" && (
          <div className={`hint${backProblem ? " warn" : ""}`}>
            {backProblem ??
              `TARS says it the next time it hears ${personName(forName!)}'s voice. It only knows ${names(voices)} by voice.`}
          </div>
        )}
      </div>
      {timer ? (
        <div className="hint">
          {`Rings for up to ${info.defaults?.timer_ring_min ?? 15} minutes, until someone turns it off.`}
        </div>
      ) : (
        <button
          type="button"
          className="tick"
          role="checkbox"
          aria-checked={f.needsAck}
          onClick={() => put({ needsAck: !f.needsAck })}
        >
          <span className="box">{f.needsAck ? <Icon name="check" size="s" /> : null}</span>
          <span>
            Say it again until someone says “got it”
            <span className="hint" style={{ display: "block" }}>
              {`Every ${info.defaults?.repeat_every_min ?? 5} minutes, up to ${info.defaults?.max_tries ?? 4} times.`}
            </span>
          </span>
        </button>
      )}
      <div className="preview-line">
        {"TARS will say "}
        <q>{`“${says()}”`}</q>
      </div>
      {f.error && (
        <div className="form-error" role="alert">
          <Icon name="alert" size="s" />
          <span>{`Couldn't set it: ${f.error.replace(/\.$/, "")}.`}</span>
        </div>
      )}
      <div className="btns">
        <button type="submit" className="btn primary big" disabled={!!problem || busy}>
          {busy ? "Setting…" : "Set it"}
        </button>
        <button type="button" className="btn ghost big" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

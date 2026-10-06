import { type FormEvent, type ReactNode, useState } from "react";
import { REMINDER_KIND_LABEL, personName, plural, reminderMeta, reminderStatus } from "../format";
import { ackReminder, cancelReminder, goConv, setReminder, snoozeReminder, useStore } from "../store";
import type { Reminder, ReminderKind, RemindersInfo } from "../types";
import { Empty, Group } from "./Blocks";

const SNOOZE_MIN = 10;
const isActive = (r: Reminder) => r.status === "scheduled" || r.status === "waiting";

export function Reminders() {
  const s = useStore();
  const info = s.reminders;
  const [adding, setAdding] = useState(false);
  if (!info?.enabled)
    return (
      <>
        <Head />
        <Empty
          title="Reminders are off"
          text="Turn them on with enabled = true under [reminders] in config.toml, then restart TARS and this page."
        />
      </>
    );
  const people = [
    ...new Set([
      ...info.voices.map(personName),
      ...s.clusters.filter(c => c.kind === "person" && c.name).map(c => personName(c.name!)),
    ]),
  ].sort();
  const active = info.reminders.filter(isActive);
  const earlier = info.reminders.filter(r => !isActive(r));
  return (
    <>
      <Head>
        {adding ? null : (
          <button className="btn primary" onClick={() => setAdding(true)}>
            New reminder
          </button>
        )}
      </Head>
      {adding && <ReminderForm info={info} people={people} onDone={() => setAdding(false)} />}
      <Group title="Active" count={active.length}>
        {active.length ? (
          <ul className="rems">
            {active.map(r => (
              <ReminderRow key={r.id} r={r} />
            ))}
          </ul>
        ) : (
          <Empty
            title="Nothing set"
            text="Say “hey TARS, set a pasta timer for twelve minutes”, or “tell Stacey dinner's ready when she's back”."
          />
        )}
      </Group>
      {earlier.length > 0 && (
        <Group title="Earlier" count={earlier.length}>
          <ul className="rems">
            {earlier.map(r => (
              <ReminderRow key={r.id} r={r} />
            ))}
          </ul>
        </Group>
      )}
    </>
  );
}

function Head({ children }: { children?: ReactNode }) {
  return (
    <div className="head">
      <div>
        <h1>Reminders</h1>
        <p>
          Timers, reminders and messages TARS says aloud, and says again until someone says “got it”. Say “hey TARS,
          remind me…”, or set one here.
        </p>
      </div>
      {children}
    </div>
  );
}

function ReminderRow({ r }: { r: Reminder }) {
  return (
    <li className={`rem ${r.status}`} data-id={r.id}>
      <div className="rem-kind">{REMINDER_KIND_LABEL[r.kind]}</div>
      <div className="rem-body">
        <p className="rem-says">“{r.says}”</p>
        <p className="rem-meta">{reminderMeta(r)}</p>
        <p className="rem-status">{reminderStatus(r)}</p>
        <div className="rem-actions">
          {r.status === "waiting" && (
            <button className="btn sm primary" onClick={() => ackReminder(r)}>
              {r.kind === "timer" ? "Turn off" : "Got it"}
            </button>
          )}
          {(r.status === "waiting" || r.status === "missed") && r.kind !== "timer" && (
            <button className="btn sm" onClick={() => snoozeReminder(r, SNOOZE_MIN)}>
              Again in {SNOOZE_MIN} min
            </button>
          )}
          {isActive(r) && !(r.kind === "timer" && r.status === "waiting") && (
            <button className="btn sm" onClick={() => cancelReminder(r)}>
              Stop…
            </button>
          )}
          {r.conversation_id != null && (
            <button className="text-action" onClick={() => goConv(r.conversation_id)}>
              Show the conversation
            </button>
          )}
        </div>
      </div>
    </li>
  );
}

type When = "in" | "at" | "back";

/** An hour from now, on the minute, as a datetime-local input wants it. */
function inAnHour() {
  const d = new Date(Date.now() + 3600_000);
  d.setSeconds(0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function ReminderForm({ info, people, onDone }: { info: RemindersInfo; people: string[]; onDone: () => void }) {
  const [kind, setKind] = useState<ReminderKind>("reminder");
  const [text, setText] = useState("");
  const [forName, setFor] = useState("");
  const [fromName, setFrom] = useState("");
  const [whenMode, setWhen] = useState<When>("in");
  const [minutes, setMinutes] = useState("10");
  const [at, setAt] = useState(inAnHour);
  const [needsAck, setNeedsAck] = useState(true);
  const [repeat, setRepeat] = useState(String(info.defaults?.repeat_every_min ?? 5));
  const [tries, setTries] = useState(String(info.defaults?.max_tries ?? 4));
  const [saving, setSaving] = useState(false);
  const known = info.voices.some(v => v.toLowerCase() === forName.trim().toLowerCase());
  const back = whenMode === "back" && known;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    const ok = await setReminder({
      kind,
      text: text.trim() || null,
      for_name: forName.trim() || null,
      from_name: kind === "timer" ? null : fromName.trim() || null,
      due: back ? null : whenMode === "at" ? new Date(at).getTime() / 1000 : Date.now() / 1000 + Number(minutes) * 60,
      when_back: back,
      needs_ack: kind === "timer" || needsAck,
      repeat_every_min: needsAck && kind !== "timer" ? Number(repeat) : null,
      max_tries: needsAck && kind !== "timer" ? Number(tries) : null,
    });
    setSaving(false);
    if (ok) onDone();
  }

  return (
    <form className="panel rem-form" onSubmit={submit} aria-label="New reminder">
      <h2>New</h2>
      <div className="seg" role="group" aria-label="Kind">
        {(["reminder", "message", "timer"] as const).map(k => (
          <button type="button" key={k} aria-pressed={kind === k} onClick={() => setKind(k)}>
            {REMINDER_KIND_LABEL[k]}
          </button>
        ))}
      </div>
      <label className="field">
        <span>{kind === "timer" ? "Label (optional)" : "What TARS says"}</span>
        <input
          value={text}
          onChange={e => setText(e.target.value)}
          placeholder={kind === "timer" ? "pasta" : kind === "message" ? "dinner's at eight" : "call the bank"}
          required={kind !== "timer"}
          maxLength={300}
        />
      </label>
      <div className="field-row">
        <label className="field">
          <span>For{kind === "message" ? "" : " (optional)"}</span>
          <input
            value={forName}
            onChange={e => setFor(e.target.value)}
            list="rem-people"
            placeholder="whoever's there"
            required={kind === "message"}
            maxLength={60}
          />
        </label>
        {kind !== "timer" && (
          <label className="field">
            <span>From (optional)</span>
            <input value={fromName} onChange={e => setFrom(e.target.value)} list="rem-people" maxLength={60} />
          </label>
        )}
        <datalist id="rem-people">
          {people.map(p => (
            <option key={p} value={p} />
          ))}
        </datalist>
      </div>
      <fieldset className="field when">
        <legend>When</legend>
        <label className="choice">
          <input type="radio" checked={whenMode === "in"} onChange={() => setWhen("in")} />
          In
          <input
            type="number"
            min={1}
            step="any"
            value={minutes}
            onChange={e => {
              setMinutes(e.target.value);
              setWhen("in");
            }}
            aria-label="Minutes from now"
          />
          minutes
        </label>
        <label className="choice">
          <input type="radio" checked={whenMode === "at"} onChange={() => setWhen("at")} />
          At
          <input
            type="datetime-local"
            value={at}
            onChange={e => {
              setAt(e.target.value);
              setWhen("at");
            }}
            aria-label="Date and time"
          />
        </label>
        <label className={`choice${known ? "" : " off"}`}>
          <input type="radio" checked={back} disabled={!known} onChange={() => setWhen("back")} />
          <span>
            {known
              ? `When ${personName(forName)} is next heard`
              : "When they're next heard (needs someone TARS knows by voice)"}
          </span>
        </label>
      </fieldset>
      {kind === "timer" ? (
        <p className="help">
          It rings until someone says “stop” or taps Turn off, for up to{" "}
          {plural(info.defaults?.timer_ring_min ?? 15, "minute")}.
        </p>
      ) : (
        <label className="choice">
          <input type="checkbox" checked={needsAck} onChange={e => setNeedsAck(e.target.checked)} />
          Say it again until someone says “got it”
        </label>
      )}
      {needsAck && kind !== "timer" && (
        <div className="field-row repeat">
          <label className="choice">
            Every
            <input type="number" min={0.5} step="any" value={repeat} onChange={e => setRepeat(e.target.value)} />
            min,
          </label>
          <label className="choice">
            up to
            <input type="number" min={1} max={30} value={tries} onChange={e => setTries(e.target.value)} />
            {plural(Number(tries) || 0, "time").replace(/^\S+ /, "")}
          </label>
        </div>
      )}
      <div className="row">
        <button className="btn primary" type="submit" disabled={saving}>
          Set it
        </button>
        <button className="btn" type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

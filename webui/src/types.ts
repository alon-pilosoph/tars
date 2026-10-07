/* The API's responses: only the fields the page reads. */

export type Label = "real" | "not_real";
export type Outcome = "answer" | "ask" | "ignore";

export interface TarsEvent {
  id: number;
  ts: number;
  kind: "wake" | "near_miss";
  outcome: Outcome | null;
  wake_score: number | null;
  heard: string | null;
  confidence: number | null;
  transcript: string | null;
  follow: "asked" | "said_nothing" | "not_for_us" | null;
  cluster_id: number | null;
  cluster_pinned: boolean;
  label: Label | null;
  auto_label: Label | null;
  auto_reason: string | null;
  has_wake_audio: boolean;
  has_request_audio: boolean;
}

export interface VoiceSample {
  event_id: number;
  transcript: string | null;
}

export interface Cluster {
  id: number;
  name: string | null;
  kind: "person" | "not_person" | "unknown";
  size: number;
  samples: VoiceSample[];
}

export interface Metric {
  name: string;
  current: string;
  candidate: string;
  lower_is_better?: boolean; // e.g. false answers; absent means higher is better
}

export interface Version {
  version: string;
  ts: number;
  active: boolean;
  note: string;
}

/** The wake model and double-check in use. */
export interface ActivePair {
  version: string;
  wake_model: string;
  threshold: number;
  check_model: string | null; // null: no double-check model, a plain phrase match
  check_window_s: number;
  replaced: string | null;
}

export interface ModelsInfo {
  active: ActivePair | null; // null when the server doesn't know which models it listens with
  results: Metric[] | null; // how the pair in use tested against the one it replaced
  history: Version[];
  problem?: string | null; // why the saved version history is being ignored
  learning: { real: number; not_real: number; missed: number; to_review: number }; // since the pair in use
}

export interface Status {
  clustering: boolean;
  enroll_at: number; // requests a named voice needs before TARS builds its voiceprint
}

export interface Speaker {
  cluster_id: number | null;
  name: string | null;
}

export interface Entry {
  text: string;
  done: boolean;
}

export interface Item {
  id: number;
  ts: number;
  kind: "link" | "note" | "list" | "file";
  title: string;
  scope: "person" | "household";
  for: Speaker | null;
  seen: boolean;
  conversation_id: number | null;
  url?: string | null; // a link's page, or where a file downloads from
  site?: string | null;
  description?: string | null;
  body?: string | null;
  entries?: Entry[];
  name?: string | null;
  mime?: string | null;
  size?: number | null;
  preview?: string | null;
}

export interface Turn {
  id: number;
  ts: number;
  role: "person" | "tars";
  text: string;
  // person turns
  has_audio?: boolean;
  speaker?: Speaker | null;
  corrected_text?: string | null;
  not_for_tars?: boolean;
  // tars turns
  rating?: "good" | "bad" | null;
  items?: number[]; // ids into State.items (the API sends whole items; load() keeps just their ids)
  timings?: Timings | null; // seconds by stage, as in the assistant's log
  answered_by?: AnsweredBy | null;
  failed_at?: FailedAt | null; // where answering failed, if it did
  error?: string | null;
}

export interface Timings {
  end_of_speech?: number; // the silence waited through
  stt?: number;
  llm?: number; // to the brain's first sentence
  tts?: number; // from that sentence to the voice's first audio
  total?: number; // from the end of speech to the first sound
}

export type AnsweredBy = "quick" | "look_up" | "fallback" | "openai";
export type FailedAt = "stt" | "llm" | "tts" | "other";

export type ApiTurn = Omit<Turn, "items"> & { items?: (Item | number)[] };

export interface ConversationWake {
  event_id: number;
  heard: string | null;
  confidence: number | null;
  outcome: Outcome | null;
  label: Label | null;
  cluster_id: number | null;
  has_request_audio: boolean;
}

export interface Conversation {
  id: number;
  started: number;
  speaker: Speaker | null;
  wake: ConversationWake | null;
  turns: Turn[];
}

export type ApiConversation = Omit<Conversation, "turns"> & { turns: ApiTurn[] };

export type ReminderKind = "timer" | "reminder" | "message";
export type ReminderStatus = "scheduled" | "waiting" | "acknowledged" | "said" | "missed" | "cancelled";

export interface Reminder {
  id: number;
  created: number;
  kind: ReminderKind;
  text: string | null; // a timer's optional label
  for_name: string | null; // null: whoever is there
  from_name: string | null;
  set_via: "voice" | "web";
  conversation_id: number | null; // where it was set, by voice
  due: number | null; // null: when for_name's voice is next heard
  needs_ack: boolean;
  repeat_every_s: number;
  max_tries: number;
  status: ReminderStatus;
  tries: number;
  next_at: number | null;
  last_said: number | null;
  acked_at: number | null;
  acked_by: string | null; // the recognized voice; null: an unknown voice, or the page
  acked_via: "voice" | "web" | null;
  says: string; // what TARS says
}

export interface RemindersInfo {
  enabled: boolean;
  reminders: Reminder[];
  voices: string[]; // names TARS knows by voice: only they can be waited for
  defaults: { repeat_every_min: number; max_tries: number; timer_ring_min: number } | null;
}

/** What the page sends to set one. */
export interface NewReminder {
  kind: ReminderKind;
  text: string | null;
  for_name: string | null;
  from_name: string | null;
  due: number | null;
  when_back: boolean;
  needs_ack: boolean;
  repeat_every_min: number | null;
  max_tries: number | null;
}

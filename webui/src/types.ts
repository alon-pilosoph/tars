export type Label = "real" | "not_real";

export interface TarsEvent {
  id: number;
  ts: number;
  kind: "wake" | "near_miss";
  outcome: "answer" | "ask" | "ignore" | null;
  wake_score: number | null;
  heard: string | null;
  confidence: number | null;
  transcript: string | null;
  follow: "asked" | "said_nothing" | "not_for_us" | null;
  speaker: string | null;
  cluster_id: number | null;
  cluster_pinned: number | boolean;
  label: Label | null;
  audio: string | null;
  utterance_audio: string | null;
  auto_label: Label | null;
  auto_reason: string | null;
}

export interface Cluster {
  id: number;
  name: string | null;
  kind: "person" | "not_person" | "unknown";
  size: number;
}

export interface Metric {
  name: string;
  current: string;
  candidate: string;
  lower_is_better?: boolean; // false answers, lookalikes let through
}

export interface Version {
  version: string;
  ts: number;
  active: boolean;
  note: string;
}

export interface Models {
  active: { version?: string; wake_model?: string; threshold?: number; check_model?: string; check_window_s?: number };
  results: Metric[] | null; // how the pair in use tested against the one it replaced
  history: Version[];
  problem?: string | null; // why the saved version history is being ignored
  learning: { real: number; not_real: number; missed: number; to_review: number }; // since the pair in use
}

export interface Status {
  events: number;
  labeled: number;
  clustering: boolean;
  unseen_items: number;
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
  turn_id: number | null;
  url?: string | null;
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
}

export type ApiTurn = Omit<Turn, "items"> & { items?: (Item | number)[] };

export interface Conversation {
  id: number;
  started: number;
  ended: number | null;
  speaker: Speaker | null;
  wake: {
    event_id: number | null;
    heard: string | null;
    confidence: number | null;
    outcome: TarsEvent["outcome"];
  } | null;
  turns: Turn[];
  preview: string | null;
  item_count?: number;
  unseen_count?: number;
}

export type ApiConversation = Omit<Conversation, "turns"> & { turns: ApiTurn[] };

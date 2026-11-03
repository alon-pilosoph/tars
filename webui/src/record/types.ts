/* A recording session, as the server sends it (webui.py's /api/session/<token>): what to say, one prompt at a time,
   recorded on TARS's own speakerphone. */

export interface Prompt {
  id: string;
  /** The exact words to say. */
  say: string;
  /** How to say them, or where from: "From across the room", "Quietly". Empty: as you'd normally talk to TARS. */
  how: string;
}

/** What the server found in a take, in case it needs doing again (recording.py's check). */
export type Problem = "nothing" | "quiet" | "loud" | "cut_off" | "short";

export interface Take {
  id: number;
  prompt: string;
  seconds: number;
  problem: Problem | null;
}

export interface Session {
  kind: string;
  /** "Your voice", "House commands". */
  title: string;
  /** Who it's for, as TARS knows them. */
  person: string;
  /** One line on why, for the first screen. */
  about: string;
  /** What happens with the recordings once they're all in, for the last screen. */
  after: string;
  prompts: Prompt[];
  /** The takes kept so far, at most one a prompt. */
  takes: Take[];
  /** Prompts skipped. */
  skipped: string[];
  minutes: number;
}

/** A take while it's being recorded: TARS stops it itself when you've stopped talking. */
export interface Recording {
  id: number;
  state: "recording" | "done";
  seconds: number;
  /** How loud it is now, 0-1, for the meter. */
  level: number;
  take: Take | null;
}

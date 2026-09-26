import type { CSSProperties } from "react";
import { type Clip, play, usePlaying } from "../audio";
import { toast } from "../store";

/** Round play button; the ring fills as the clip plays. */
export function PlayButton({
  clip,
  label,
  sm,
  title,
}: {
  clip: Clip;
  label: string;
  sm?: boolean;
  title?: string | null;
}) {
  const { on, p } = usePlaying(clip);
  return (
    <button
      className={`play${sm ? " sm" : ""}${on ? " on" : ""}`}
      style={on && p != null ? ({ "--p": p } as CSSProperties) : undefined}
      aria-label={`${on ? "Stop" : "Play"} ${label}`}
      title={title ?? undefined}
      onClick={() => play(clip, () => toast("Couldn't play that clip."))}
    />
  );
}

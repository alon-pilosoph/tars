import type { CSSProperties } from "react";
import { type Clip, play, useProgress } from "../audio";
import { toast } from "../store";

export function PlayButton({
  clip,
  label,
  small,
  title,
}: {
  clip: Clip;
  label: string;
  small?: boolean;
  title?: string | null;
}) {
  const progress = useProgress(clip);
  const on = progress !== null;
  return (
    <button
      className={`play${small ? " sm" : ""}${on ? " on" : ""}`}
      style={on ? ({ "--p": progress } as CSSProperties) : undefined}
      aria-label={`${on ? "Stop" : "Play"} ${label}`}
      title={title ?? undefined}
      onClick={() => play(clip, () => toast("Couldn't play that clip."))}
    />
  );
}

/** Which turns of a conversation show. Long ones fold their middle, but a turn that sent something, or is being
    corrected, always shows. Returns the turns before the fold, how many are folded, and the turns after it. */
export function fold<T>(turns: T[], open: boolean, keep: (t: T) => boolean) {
  const shows = (t: T, i: number) => open || turns.length <= 4 || i < 2 || i === turns.length - 1 || keep(t);
  const first = turns.findIndex((t, i) => !shows(t, i));
  if (first < 0) return { head: turns, hidden: 0, tail: [] as T[] };
  const rest = turns.slice(first);
  return {
    head: turns.slice(0, first),
    hidden: rest.filter((t, i) => !shows(t, first + i)).length,
    tail: rest.filter((t, i) => shows(t, first + i)),
  };
}

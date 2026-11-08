// Wins against losses as one bar, with the counts in words next to it.
export default function SplitBar({ wins, losses }) {
  const total = wins + losses;
  if (!total) return null;
  const word = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  return (
    <div>
      <div
        className="flex h-3.5 overflow-hidden rounded-full bg-surface-2"
        role="img"
        aria-label={`${word(wins, "win", "wins")} and ${word(losses, "loss", "losses")}`}
      >
        <i className="block h-full bg-pos-bar" style={{ width: `${(wins / total) * 100}%` }} />
        <i className="block h-full bg-neg-bar" style={{ width: `${(losses / total) * 100}%` }} />
      </div>
      <p className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        <b className="font-semibold text-pos">{word(wins, "win", "wins")}</b>
        <b className="font-semibold text-neg">{word(losses, "loss", "losses")}</b>
        <span className="text-muted">of {total} closed {total === 1 ? "trade" : "trades"}</span>
      </p>
    </div>
  );
}

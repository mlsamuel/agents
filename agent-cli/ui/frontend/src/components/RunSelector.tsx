import type { Run } from "../types";

interface Props {
  runs: Run[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}

function fmt(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function RunSelector({ runs, selectedId, onSelect }: Props) {
  if (runs.length === 0) return null;
  return (
    <div className="run-selector">
      <label htmlFor="run-select">Run</label>
      <select
        id="run-select"
        value={selectedId ?? ""}
        onChange={(e) => onSelect(Number(e.target.value))}
      >
        {runs.map((r) => (
          <option key={r.id} value={r.id}>
            {fmt(r.run_at)} · {r.total ?? "?"} emails
            {r.avg_overall != null ? ` · avg ${r.avg_overall.toFixed(1)}/5` : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

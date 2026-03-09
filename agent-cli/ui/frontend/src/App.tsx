import { useEffect, useState } from "react";
import type { Run, Result } from "./types";
import RunSelector from "./components/RunSelector";
import ResultCard from "./components/ResultCard";

export default function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedRun, setSelectedRun] = useState<Run | null>(null);
  const [results, setResults] = useState<Result[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/runs")
      .then((r) => r.json())
      .then((data: Run[]) => {
        setRuns(data);
        if (data.length > 0) setSelectedRun(data[0]);
      })
      .catch(() => setError("Could not connect to backend at http://localhost:8000"));
  }, []);

  useEffect(() => {
    if (!selectedRun) return;
    setLoading(true);
    setResults([]);
    fetch(`/api/runs/${selectedRun.id}/results`)
      .then((r) => r.json())
      .then((data: Result[]) => setResults(data))
      .catch(() => setError("Failed to load results"))
      .finally(() => setLoading(false));
  }, [selectedRun]);

  const handleSelectRun = (id: number) => {
    const run = runs.find((r) => r.id === id) ?? null;
    setSelectedRun(run);
  };

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <h1>agent-cli showcase</h1>
          <span className="header-sub">Claude · CLI transport · eval results</span>
        </div>
        <div className="header-right">
          <RunSelector runs={runs} selectedId={selectedRun?.id ?? null} onSelect={handleSelectRun} />
          {selectedRun && selectedRun.avg_overall != null && (
            <div className="run-stats">
              <span>{selectedRun.total} emails</span>
              <span>avg {selectedRun.avg_overall.toFixed(1)} / 5</span>
            </div>
          )}
        </div>
      </header>

      <main className="app-main">
        {error && <div className="error-banner">{error}</div>}
        {!error && runs.length === 0 && (
          <div className="empty-state">
            No runs yet. Run the pipeline first:<br />
            <code>python pipeline.py --limit 20 --no-improve</code>
          </div>
        )}
        {loading && <div className="loading">Loading results…</div>}
        {results.map((r) => (
          <ResultCard key={r.id} result={r} />
        ))}
      </main>
    </div>
  );
}

import { useEffect, useState } from "react";
import { Escalation } from "./types";
import EscalationCard from "./components/EscalationCard";
import DecisionModal from "./components/DecisionModal";
import ResolvedList from "./components/ResolvedList";

export default function App() {
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [modalThread, setModalThread] = useState<Escalation | null>(null);

  async function fetchEscalations() {
    try {
      const res = await fetch("/api/escalations");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: Escalation[] = await res.json();
      setEscalations(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch escalations");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchEscalations();
    const interval = setInterval(fetchEscalations, 5000);
    return () => clearInterval(interval);
  }, []);

  async function handleDecide(threadId: string, decision: string) {
    setSubmitting(threadId);
    setModalThread(null);
    try {
      const res = await fetch(`/api/escalations/${encodeURIComponent(threadId)}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: "Unknown error" }));
        throw new Error(detail.detail ?? `HTTP ${res.status}`);
      }
      // Optimistically move card to "decided" — polling will update to approved/overridden
      // once pipeline --serve resumes the thread
      setEscalations((prev) =>
        prev.map((e) =>
          e.thread_id === threadId
            ? { ...e, status: "decided", human_decision: decision, decided_at: new Date().toISOString() }
            : e
        )
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decision failed");
    } finally {
      setSubmitting(null);
    }
  }

  const pending = escalations.filter((e) => e.status === "pending");
  const resolved = escalations.filter((e) => e.status !== "pending"); // decided | approved | overridden

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <h1>Escalation Review</h1>
          <span className="header-sub">
            {pending.length} pending · {resolved.length} resolved
          </span>
        </div>
      </header>

      <main className="app-main">
        {error && <div className="error-banner">{error}</div>}

        {loading ? (
          <div className="loading">Loading…</div>
        ) : pending.length === 0 && resolved.length === 0 ? (
          <div className="empty-state">
            No escalations yet.
            <br />
            <code>python pipeline.py --limit 1</code>
          </div>
        ) : (
          <>
            {pending.length > 0 && (
              <>
                <div className="section-heading">Pending review ({pending.length})</div>
                {pending.map((e) => (
                  <EscalationCard
                    key={e.thread_id}
                    escalation={e}
                    submitting={submitting === e.thread_id}
                    onApprove={() => handleDecide(e.thread_id, "approve")}
                    onOverride={() => setModalThread(e)}
                  />
                ))}
              </>
            )}

            {resolved.length > 0 && (
              <ResolvedList escalations={resolved} />
            )}
          </>
        )}
      </main>

      {modalThread && (
        <DecisionModal
          escalation={modalThread}
          onConfirm={(text) => handleDecide(modalThread.thread_id, text)}
          onCancel={() => setModalThread(null)}
        />
      )}
    </div>
  );
}

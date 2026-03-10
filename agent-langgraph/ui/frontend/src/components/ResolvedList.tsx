import { useState } from "react";
import { Escalation } from "../types";

interface Props {
  escalations: Escalation[];
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function ResolvedList({ escalations }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <div>
      <button className="resolved-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▲" : "▼"} Resolved ({escalations.length})
      </button>

      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {escalations.map((e) => (
            <div key={e.thread_id} className="result-card">
              <div className="result-header" style={{ cursor: "default" }}>
                <div className="result-meta">
                  <span className="result-subject">{e.subject}</span>
                  <span className={`status-badge status-${e.status}`}>{e.status}</span>
                </div>
                <div className="result-tags">
                  {e.queue && <span className="tag">{e.queue}</span>}
                  {e.priority && <span className="tag">{e.priority}</span>}
                  {e.escalated_agents.map((a) => (
                    <span key={a} className="tag skill">{a}</span>
                  ))}
                  {e.decided_at && (
                    <span className="tag">decided {formatDate(e.decided_at)}</span>
                  )}
                </div>
                {e.human_decision && (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>
                    "{e.human_decision}"
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

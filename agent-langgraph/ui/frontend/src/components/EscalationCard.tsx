import { useState } from "react";
import { Escalation } from "../types";

interface Props {
  escalation: Escalation;
  submitting: boolean;
  onApprove: () => void;
  onOverride: () => void;
}

function age(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function EscalationCard({ escalation: e, submitting, onApprove, onOverride }: Props) {
  const [expanded, setExpanded] = useState(true);

  return (
    <div className="result-card">
      <div className="result-header" onClick={() => setExpanded((v) => !v)}>
        <div className="result-meta">
          <span className="result-subject">{e.subject}</span>
          <span className={`status-badge status-${e.status}`}>{e.status}</span>
        </div>
        <div className="result-tags">
          {e.queue && <span className="tag">{e.queue}</span>}
          {e.priority && <span className="tag">{e.priority}</span>}
          {e.email_type && <span className="tag">{e.email_type}</span>}
          {e.escalated_agents.map((a) => (
            <span key={a} className="tag skill">{a}</span>
          ))}
          <span className="tag">{age(e.created_at)}</span>
        </div>
        <span className="expand-toggle">{expanded ? "▲" : "▼"}</span>
      </div>

      {expanded && (
        <div className="result-body">
          {e.body && (
            <div className="reply-col">
              <div className="reply-col-label">Customer email</div>
              <div className="reply-text">{e.body}</div>
            </div>
          )}

          {e.escalated_agents.length > 0 && (
            <div className="reply-columns">
              {e.escalated_agents.map((agent, i) => (
                <div key={agent} style={{ display: "contents" }}>
                  {e.summaries[i] && (
                    <div className="reply-col">
                      <div className="reply-col-label">{agent} — summary</div>
                      <div className="reply-text">{e.summaries[i] || "(none)"}</div>
                    </div>
                  )}
                  {e.draft_replies[i] && (
                    <div className="reply-col">
                      <div className="reply-col-label">{agent} — draft reply</div>
                      <div className="reply-text">{e.draft_replies[i] || "(none)"}</div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {e.status === "pending" && (
            <div className="card-actions">
              <button
                className="btn btn-approve"
                disabled={submitting}
                onClick={(ev) => { ev.stopPropagation(); onApprove(); }}
              >
                {submitting ? "Approving…" : "Approve"}
              </button>
              <button
                className="btn btn-override"
                disabled={submitting}
                onClick={(ev) => { ev.stopPropagation(); onOverride(); }}
              >
                Override…
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

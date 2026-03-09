import { useState } from "react";
import type { Result } from "../types";

interface Props {
  result: Result;
}

function scoreClass(v: number | null): string {
  if (v == null) return "";
  if (v <= 2) return "score-red";
  if (v === 3) return "score-amber";
  return "score-green";
}

function ScorePill({ label, value }: { label: string; value: number | null }) {
  return (
    <span className={`score-pill ${scoreClass(value)}`}>
      {label} {value ?? "—"}/5
    </span>
  );
}

export default function ResultCard({ result }: Props) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="result-card">
      <div className="result-header" onClick={() => setExpanded((e) => !e)}>
        <div className="result-meta">
          <span className="result-index">#{result.email_index}</span>
          <span className="result-subject">{result.subject}</span>
          {result.score_avg != null && (
            <span className={`avg-badge ${scoreClass(Math.round(result.score_avg))}`}>
              {result.score_avg.toFixed(1)} / 5
            </span>
          )}
        </div>
        <div className="result-tags">
          <span className="tag">{result.queue}</span>
          <span className="tag">{result.email_type}</span>
          <span className="tag">{result.priority}</span>
          {result.skills && <span className="tag skill">{result.skills}</span>}
        </div>
        <div className="result-scores">
          <ScorePill label="action" value={result.score_action} />
          <ScorePill label="completeness" value={result.score_completeness} />
          <ScorePill label="tone" value={result.score_tone} />
          {result.score_comment && result.score_comment !== "none" && (
            <span className="score-comment">"{result.score_comment}"</span>
          )}
        </div>
        <span className="expand-toggle">{expanded ? "▲" : "▼"}</span>
      </div>

      {expanded && (
        <div className="result-body">
          <div className="reply-columns">
            <div className="reply-col">
              <div className="reply-col-label">Ground truth</div>
              <pre className="reply-text">{result.ground_truth}</pre>
            </div>
            <div className="reply-col">
              <div className="reply-col-label">Generated</div>
              <pre className="reply-text">{result.generated}</pre>
            </div>
          </div>
          {result.tools && (
            <p className="tools-line">Tools used: {result.tools}</p>
          )}
        </div>
      )}
    </div>
  );
}

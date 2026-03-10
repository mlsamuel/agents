import { useState } from "react";
import { Escalation } from "../types";

interface Props {
  escalation: Escalation;
  onConfirm: (decision: string) => void;
  onCancel: () => void;
}

export default function DecisionModal({ escalation: e, onConfirm, onCancel }: Props) {
  const [text, setText] = useState("override: ");

  function handleConfirm() {
    const trimmed = text.trim();
    if (trimmed) onConfirm(trimmed);
  }

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal" onClick={(ev) => ev.stopPropagation()}>
        <h2>Override decision</h2>
        <p>
          Provide guidance for <strong>{e.subject}</strong>. The pipeline will
          resume with your instruction instead of the agent's draft.
        </p>
        <textarea
          autoFocus
          value={text}
          onChange={(ev) => setText(ev.target.value)}
          onKeyDown={(ev) => {
            if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) handleConfirm();
            if (ev.key === "Escape") onCancel();
          }}
        />
        <div className="modal-actions">
          <button className="btn btn-cancel" onClick={onCancel}>Cancel</button>
          <button
            className="btn btn-override"
            disabled={!text.trim()}
            onClick={handleConfirm}
          >
            Send override
          </button>
        </div>
      </div>
    </div>
  );
}

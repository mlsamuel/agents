"""
export_showcase.py — Generate a self-contained static showcase HTML.

Two tabs:
  1. Eval results   — latest pipeline run (pipeline_results table)
  2. Escalation UI  — escalation_queue table (human-in-the-loop review)

Falls back to sample data if either table is empty.

Usage:
    cd agent-langgraph
    python ui/export_showcase.py
    open ui/showcase/index.html
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

OUT = Path(__file__).parent / "showcase" / "index.html"

# ── Sample data (used when tables are empty) ──────────────────────────────────

SAMPLE_RESULTS = [
    {
        "email_index": 1,
        "subject": "Cannot access account management portal — urgent",
        "body": "Hi, I have been unable to log into the account management portal for the past 3 hours. I keep getting a 503 error. This is affecting our entire team of 12 people and we have a critical deployment scheduled for this afternoon. Please help urgently.",
        "queue": "Technical Support",
        "email_type": "Incident",
        "priority": "critical",
        "skills": "diagnose_incident",
        "tools": "lookup_customer, search_knowledge_base, get_ticket_history, create_ticket, send_reply",
        "ground_truth": "Thank you for reaching out. We are aware of an ongoing issue with the account management portal and our engineering team is actively working on a fix. The issue is expected to be resolved within the next 2 hours. We apologise for the inconvenience.",
        "generated": "Thank you for reaching out. I can see you're experiencing a 503 error with our account management portal. I've created ticket #TKT-482901 to track this issue for your team.\n\nOur engineering team is currently investigating a broader connectivity issue affecting some enterprise accounts. Given the critical nature of your scheduled deployment, I've escalated your ticket to our senior infrastructure team for priority handling.\n\nIf you have any further questions, please let us know.",
        "score_action": 4,
        "score_completeness": 4,
        "score_tone": 5,
        "score_avg": 4.3,
        "score_comment": "Correctly created ticket and escalated. Could acknowledge the outage more directly.",
    },
    {
        "email_index": 2,
        "subject": "Refund request for duplicate charge — order #ORD-77423",
        "body": "Hello, I was charged twice for my order #ORD-77423 on 12 March. Both charges appeared on my credit card statement. Please issue a refund for the duplicate charge as soon as possible.",
        "queue": "Billing and Payments",
        "email_type": "Request",
        "priority": "high",
        "skills": "process_refund",
        "tools": "lookup_customer, check_order_status, process_refund, send_reply",
        "ground_truth": "Thank you for reaching out. I can confirm that order #ORD-77423 was charged twice in error. A full refund of $89.99 has been processed to your original payment method and should appear within 3–5 business days.",
        "generated": "Thank you for reaching out. I've looked into your order #ORD-77423 and can confirm a duplicate charge was applied on 12 March. I've processed a refund of $89.99 to your original payment method — please allow 3–5 business days for it to appear on your statement.\n\nIf you have any further questions, please let us know.",
        "score_action": 5,
        "score_completeness": 5,
        "score_tone": 5,
        "score_avg": 5.0,
        "score_comment": "Perfect match — correct amount, correct timeline, professional tone.",
    },
    {
        "email_index": 3,
        "subject": "Return request — wrong item received",
        "body": "I ordered a size L blue polo shirt (item #POL-8812) but received a size M red polo instead. I need to return it and get the correct item.",
        "queue": "Returns and Exchanges",
        "email_type": "Request",
        "priority": "medium",
        "skills": "process_return",
        "tools": "lookup_customer, check_order_status, initiate_return, send_reply",
        "ground_truth": "Thank you for letting us know. We're sorry about the mix-up. I've initiated a return label which will be emailed to you shortly. Once we receive the incorrect item, we'll ship your size L blue polo within 1–2 business days.",
        "generated": "Thank you for reaching out. I'm sorry to hear you received the wrong item. I've checked your order and can confirm a size M red polo was shipped in error. I've initiated a return with prepaid label #RTN-20341 — you'll receive it by email within the hour.\n\nOnce we receive the incorrect item, your size L blue polo (item #POL-8812) will be dispatched within 1–2 business days. If you have any further questions, please let us know.",
        "score_action": 5,
        "score_completeness": 5,
        "score_tone": 4,
        "score_avg": 4.7,
        "score_comment": "Complete and accurate. Slightly wordy but professional.",
    },
]

SAMPLE_RUN = {
    "id": 1,
    "run_at": "2026-03-10T09:14:00+00:00",
    "total": 3,
    "avg_overall": 4.67,
}

SAMPLE_ESCALATIONS = [
    {
        "thread_id": "email-42-1",
        "subject": "Complete outage of account management portal — enterprise customer",
        "body": "Hi Support,\n\nOur entire organisation has been locked out of the account management portal since 08:00 this morning. We are an enterprise customer (account ID ENT-00291) and this is severely impacting our business operations. We have a board presentation in 2 hours and need access restored immediately.\n\nThis is the third time this month we have experienced access issues. We expect better reliability given our contract SLA.\n\nRegards,\nSarah Chen\nHead of IT, Meridian Group",
        "queue": "Technical Support",
        "priority": "critical",
        "email_type": "Incident",
        "escalated_agents": ["technical_support"],
        "summaries": [
            "Enterprise customer (ENT-00291) reporting complete portal outage since 08:00. Third recurrence this month. SLA concerns raised. No active outage confirmed in knowledge base. Ticket TKT-482901 created. Escalated due to enterprise tier and critical priority."
        ],
        "draft_replies": [
            "Thank you for reaching out, Sarah.\n\nI sincerely apologise for the disruption you and your team are experiencing. I have logged this as ticket #TKT-482901 and escalated it directly to our senior enterprise support team given the urgency and your organisation's SLA.\n\nA specialist will be in contact with you within the next 30 minutes. In the meantime, could you confirm whether the issue affects all users in your organisation or specific accounts, and share any error messages you are seeing? This will help our team resolve the issue faster.\n\nI understand the timing is particularly difficult given your upcoming presentation, and we are treating this as our highest priority.\n\nIf you have any further questions, please let us know."
        ],
        "status": "pending",
        "human_decision": None,
        "created_at": "2026-03-10T09:14:22+00:00",
        "decided_at": None,
    },
    {
        "thread_id": "email-38-2",
        "subject": "Suspected data breach — unauthorised login attempts",
        "body": "We have detected multiple unauthorised login attempts on our admin accounts over the last 6 hours. Our security team flagged 47 failed attempts from IPs in Eastern Europe. We believe our credentials may have been compromised. Please advise immediately.",
        "queue": "Technical Support",
        "priority": "critical",
        "email_type": "Incident",
        "escalated_agents": ["technical_support"],
        "summaries": [
            "Security incident: 47 unauthorised login attempts detected on admin accounts. Potential credential compromise. Escalated per security incident policy."
        ],
        "draft_replies": [
            "Thank you for reaching out.\n\nWe take security incidents extremely seriously. I have immediately escalated this to our security response team and created ticket #TKT-480033. Our team will contact you within 15 minutes.\n\nAs an immediate precaution, please reset all admin account passwords and enable two-factor authentication if not already active. Do not share any credentials over email.\n\nIf you have any further questions, please let us know."
        ],
        "status": "approved",
        "human_decision": "approve",
        "created_at": "2026-03-10T08:47:11+00:00",
        "decided_at": "2026-03-10T08:52:33+00:00",
    },
]


# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
:root {
  --bg: #f5f6f8;
  --surface: #ffffff;
  --surface-2: #f0f1f4;
  --border: #dde0e8;
  --text: #1a1d2e;
  --text-muted: #6b6f85;
  --accent: #4361ee;
  --green: #16a34a;
  --amber: #b45309;
  --red: #dc2626;
  --green-bg: rgba(22, 163, 74, 0.10);
  --amber-bg: rgba(180, 83, 9, 0.10);
  --red-bg: rgba(220, 38, 38, 0.10);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  color: var(--text);
  background: var(--bg);
}
* { box-sizing: border-box; margin: 0; padding: 0; }

.app { min-height: 100vh; display: flex; flex-direction: column; }

/* Header */
.app-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 24px; border-bottom: 1px solid var(--border);
  background: var(--surface); position: sticky; top: 0; z-index: 10;
}
.header-left h1 { font-size: 17px; font-weight: 600; }
.header-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; display: block; }
.header-right { display: flex; align-items: center; gap: 16px; font-size: 13px; color: var(--text-muted); }

/* Tabs */
.tabs {
  display: flex; gap: 0; border-bottom: 1px solid var(--border);
  background: var(--surface); padding: 0 24px;
}
.tab-btn {
  padding: 10px 18px; font-size: 13px; font-weight: 500; cursor: pointer;
  border: none; background: none; color: var(--text-muted);
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  font-family: inherit;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

/* Main */
.app-main {
  padding: 20px 24px; max-width: 1200px; margin: 0 auto;
  width: 100%; display: flex; flex-direction: column; gap: 10px;
}
.tab-pane { display: none; }
.tab-pane.active { display: contents; }

/* Section heading */
.section-heading {
  font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-muted); padding: 8px 0 4px;
}

/* Result card */
.result-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden;
}
.result-header {
  padding: 14px 16px; cursor: pointer;
  display: flex; flex-direction: column; gap: 6px;
  user-select: none; position: relative;
}
.result-header:hover { background: var(--surface-2); }
.result-meta { display: flex; align-items: baseline; gap: 10px; }
.result-index { font-size: 12px; color: var(--text-muted); min-width: 24px; }
.result-subject { font-weight: 500; font-size: 14px; flex: 1; }
.result-tags { display: flex; flex-wrap: wrap; gap: 6px; }

.tag {
  font-size: 11px; padding: 2px 7px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 4px; color: var(--text-muted);
}
.tag.skill { color: var(--accent); border-color: rgba(107,138,253,0.3); background: rgba(107,138,253,0.08); }

.expand-toggle { position: absolute; right: 16px; top: 16px; font-size: 11px; color: var(--text-muted); }

/* Result body */
.result-body { border-top: 1px solid var(--border); padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.result-body.hidden { display: none; }

.reply-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.reply-col { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.reply-col-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-muted);
  padding: 8px 12px; border-bottom: 1px solid var(--border);
}
.reply-text { padding: 12px; font-family: inherit; font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.tools-line { font-size: 12px; color: var(--text-muted); }

/* Score badges */
.avg-badge { font-size: 13px; font-weight: 600; padding: 2px 8px; border-radius: 12px; }
.score-pills { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.score-pill { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 500; }
.score-comment { font-size: 12px; color: var(--text-muted); font-style: italic; }
.score-green { color: var(--green); background: var(--green-bg); }
.score-amber { color: var(--amber); background: var(--amber-bg); }
.score-red   { color: var(--red);   background: var(--red-bg); }

/* Status badges */
.status-badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; }
.status-pending    { color: var(--amber); background: var(--amber-bg); }
.status-decided    { color: var(--text-muted); background: var(--surface-2); border: 1px solid var(--border); }
.status-approved   { color: var(--green); background: var(--green-bg); }
.status-overridden { color: var(--accent); background: rgba(67,97,238,0.08); }

/* Action buttons */
.btn { padding: 6px 14px; border-radius: 6px; border: none; cursor: default; font-size: 13px; font-weight: 500; font-family: inherit; opacity: 0.85; }
.btn-approve { background: var(--green); color: #fff; }
.btn-override { background: var(--amber-bg); color: var(--amber); border: 1px solid var(--amber); }
.btn-cancel { background: var(--surface-2); color: var(--text-muted); border: 1px solid var(--border); }
.card-actions { display: flex; gap: 8px; padding-top: 4px; }

/* Modal (static) */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal {
  background: var(--surface); border-radius: 10px; padding: 24px;
  width: 560px; max-width: 90vw; display: flex; flex-direction: column; gap: 12px;
}
.modal h2 { font-size: 15px; font-weight: 600; }
.modal p { font-size: 13px; color: var(--text-muted); }
.modal textarea {
  width: 100%; height: 120px; border: 1px solid var(--border); border-radius: 6px;
  padding: 10px; font-family: inherit; font-size: 13px; resize: vertical;
  background: var(--surface); color: var(--text);
}
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

/* Resolved toggle */
.resolved-toggle {
  background: none; border: none; cursor: pointer; font-size: 13px;
  color: var(--text-muted); padding: 8px 0; display: flex; align-items: center;
  gap: 6px; font-family: inherit;
}

/* Footer */
.generated-at { text-align: center; padding: 32px; font-size: 12px; color: var(--text-muted); }
"""


# ── JS ────────────────────────────────────────────────────────────────────────

JS = """
function scoreClass(v) {
  if (v == null) return '';
  if (v <= 2) return 'score-red';
  if (v <= 3) return 'score-amber';
  return 'score-green';
}
function esc(s) {
  return (s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString(undefined, {
    year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'
  });
}
function age(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h ago';
  return Math.floor(hrs / 24) + 'd ago';
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === name));
}

// ── Toggle card body ──────────────────────────────────────────────────────────
function toggle(header) {
  const body = header.nextElementSibling;
  const arrow = header.querySelector('.expand-toggle');
  body.classList.toggle('hidden');
  arrow.textContent = body.classList.contains('hidden') ? '▼' : '▲';
}

// ── Modal (static demo) ───────────────────────────────────────────────────────
function showModal(subject) {
  document.getElementById('modal-subject').textContent = subject;
  document.getElementById('modal-overlay').style.display = 'flex';
}
function hideModal() {
  document.getElementById('modal-overlay').style.display = 'none';
}

// ── Build eval result cards ───────────────────────────────────────────────────
function buildEvalCard(r) {
  const avgCls = scoreClass(r.score_avg ? Math.round(r.score_avg) : null);
  const avgStr = r.score_avg != null ? r.score_avg.toFixed(1) + ' / 5' : '—';
  const comment = (r.score_comment && r.score_comment !== 'none')
    ? '<span class="score-comment">&ldquo;' + esc(r.score_comment) + '&rdquo;</span>' : '';
  return `
<div class="result-card">
  <div class="result-header" onclick="toggle(this)">
    <div class="result-meta">
      <span class="result-index">#${r.email_index}</span>
      <span class="result-subject">${esc(r.subject)}</span>
      <span class="avg-badge ${avgCls}">${avgStr}</span>
    </div>
    <div class="result-tags">
      <span class="tag">${esc(r.queue)}</span>
      <span class="tag">${esc(r.email_type)}</span>
      <span class="tag">${esc(r.priority)}</span>
      ${r.skills ? '<span class="tag skill">' + esc(r.skills) + '</span>' : ''}
    </div>
    <div class="score-pills">
      <span class="score-pill ${scoreClass(r.score_action)}">action ${r.score_action ?? '—'}/5</span>
      <span class="score-pill ${scoreClass(r.score_completeness)}">completeness ${r.score_completeness ?? '—'}/5</span>
      <span class="score-pill ${scoreClass(r.score_tone)}">tone ${r.score_tone ?? '—'}/5</span>
      ${comment}
    </div>
    <span class="expand-toggle">▲</span>
  </div>
  <div class="result-body">
    <div class="reply-columns">
      <div class="reply-col">
        <div class="reply-col-label">Ground truth</div>
        <pre class="reply-text">${esc(r.ground_truth)}</pre>
      </div>
      <div class="reply-col">
        <div class="reply-col-label">Generated</div>
        <pre class="reply-text">${esc(r.generated)}</pre>
      </div>
    </div>
    ${r.tools ? '<p class="tools-line">Tools: ' + esc(r.tools) + '</p>' : ''}
  </div>
</div>`;
}

// ── Build escalation card ─────────────────────────────────────────────────────
function buildEscalationCard(e, expanded) {
  const agentCols = e.escalated_agents.map((agent, i) => {
    const summary     = e.summaries[i] || '';
    const draftReply  = e.draft_replies[i] || '';
    return `
      ${summary ? '<div class="reply-col"><div class="reply-col-label">' + esc(agent) + ' — summary</div><div class="reply-text">' + esc(summary) + '</div></div>' : ''}
      ${draftReply ? '<div class="reply-col"><div class="reply-col-label">' + esc(agent) + ' — draft reply</div><div class="reply-text">' + esc(draftReply) + '</div></div>' : ''}`;
  }).join('');

  const actions = e.status === 'pending' ? `
    <div class="card-actions">
      <button class="btn btn-approve" onclick="event.stopPropagation(); alert('Static showcase — connect the live UI at http://localhost:5173')">Approve</button>
      <button class="btn btn-override" onclick="event.stopPropagation(); showModal(${JSON.stringify(e.subject)})">Override…</button>
    </div>` : (e.human_decision ? `<p class="tools-line">Decision: ${esc(e.human_decision)}${e.decided_at ? ' · ' + fmtDate(e.decided_at) : ''}</p>` : '');

  const bodyHidden = expanded ? '' : ' hidden';
  return `
<div class="result-card">
  <div class="result-header" onclick="toggle(this)">
    <div class="result-meta">
      <span class="result-subject">${esc(e.subject)}</span>
      <span class="status-badge status-${esc(e.status)}">${esc(e.status)}</span>
    </div>
    <div class="result-tags">
      ${e.queue ? '<span class="tag">' + esc(e.queue) + '</span>' : ''}
      ${e.priority ? '<span class="tag">' + esc(e.priority) + '</span>' : ''}
      ${e.email_type ? '<span class="tag">' + esc(e.email_type) + '</span>' : ''}
      ${e.escalated_agents.map(a => '<span class="tag skill">' + esc(a) + '</span>').join('')}
      <span class="tag">${age(e.created_at)}</span>
    </div>
    <span class="expand-toggle">${expanded ? '▲' : '▼'}</span>
  </div>
  <div class="result-body${bodyHidden}">
    ${e.body ? '<div class="reply-col"><div class="reply-col-label">Customer email</div><div class="reply-text">' + esc(e.body) + '</div></div>' : ''}
    <div class="reply-columns">${agentCols}</div>
    ${actions}
  </div>
</div>`;
}

// ── Render ────────────────────────────────────────────────────────────────────
(function () {
  const run   = DATA.run;
  const results = DATA.results;
  const escalations = DATA.escalations;

  // Header
  const pending  = escalations.filter(e => e.status === 'pending');
  const resolved = escalations.filter(e => e.status !== 'pending');

  document.getElementById('header-title').textContent =
    run.total + ' emails · avg ' + (run.avg_overall ? run.avg_overall.toFixed(1) + '/5' : '—') +
    ' · ' + pending.length + ' escalation' + (pending.length !== 1 ? 's' : '') + ' pending';
  document.getElementById('header-date').textContent = fmtDate(run.run_at);

  // Eval tab
  const evalPane = document.getElementById('pane-eval');
  evalPane.innerHTML = results.map(buildEvalCard).join('') +
    '<p class="generated-at">Generated ' + new Date().toLocaleString() + '</p>';

  // Escalation tab
  const hitlPane = document.getElementById('pane-hitl');
  let hitlHtml = '';
  if (pending.length > 0) {
    hitlHtml += '<div class="section-heading">Pending review (' + pending.length + ')</div>';
    hitlHtml += pending.map(e => buildEscalationCard(e, true)).join('');
  }
  if (resolved.length > 0) {
    hitlHtml += '<div class="section-heading" style="margin-top:8px">Resolved (' + resolved.length + ')</div>';
    hitlHtml += resolved.map(e => buildEscalationCard(e, false)).join('');
  }
  if (!hitlHtml) {
    hitlHtml = '<div style="text-align:center;padding:60px 20px;color:var(--text-muted)">No escalations yet.</div>';
  }
  hitlHtml += '<p class="generated-at">Generated ' + new Date().toLocaleString() + '</p>';
  hitlPane.innerHTML = hitlHtml;
})();
"""


# ── HTML template ─────────────────────────────────────────────────────────────

def html_template(run: dict, results: list, escalations: list) -> str:
    data_js = json.dumps(
        {"run": run, "results": results, "escalations": escalations},
        default=str,
        ensure_ascii=False,
        indent=None,
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>agent-langgraph showcase</title>
  <style>{CSS}</style>
</head>
<body>
<div class="app">
  <header class="app-header">
    <div class="header-left">
      <h1>agent-langgraph showcase</h1>
      <span class="header-sub" id="header-title"></span>
    </div>
    <div class="header-right">
      <span id="header-date"></span>
    </div>
  </header>

  <nav class="tabs">
    <button class="tab-btn active" data-tab="eval" onclick="switchTab('eval')">Eval results</button>
    <button class="tab-btn" data-tab="hitl" onclick="switchTab('hitl')">Escalation review</button>
  </nav>

  <main class="app-main">
    <div class="tab-pane active" data-pane="eval" id="pane-eval"></div>
    <div class="tab-pane" data-pane="hitl" id="pane-hitl"></div>
  </main>
</div>

<!-- Static override modal demo -->
<div class="modal-overlay" id="modal-overlay" style="display:none" onclick="hideModal()">
  <div class="modal" onclick="event.stopPropagation()">
    <h2>Override decision</h2>
    <p>Provide guidance for <strong id="modal-subject"></strong>. The pipeline will resume with your instruction.</p>
    <textarea>override: </textarea>
    <div class="modal-actions">
      <button class="btn btn-cancel" onclick="hideModal()">Cancel</button>
      <button class="btn btn-override" onclick="alert('Static showcase — connect the live UI at http://localhost:5173'); hideModal()">Send override</button>
    </div>
  </div>
</div>

<script>const DATA = {data_js};</script>
<script>{JS}</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set — using sample data")
        run, results, escalations = SAMPLE_RUN, SAMPLE_RESULTS, SAMPLE_ESCALATIONS
    else:
        try:
            pool = await asyncpg.create_pool(url)
            async with pool.acquire() as conn:
                run_row = await conn.fetchrow(
                    "SELECT * FROM pipeline_runs ORDER BY run_at DESC LIMIT 1"
                )
                if run_row:
                    run = dict(run_row)
                    result_rows = await conn.fetch(
                        """SELECT email_index, subject, body, queue, email_type, priority,
                                  skills, tools, ground_truth, generated,
                                  score_action, score_completeness, score_tone, score_avg, score_comment
                           FROM pipeline_results WHERE run_id = $1 ORDER BY email_index""",
                        run_row["id"],
                    )
                    results = [dict(r) for r in result_rows] or SAMPLE_RESULTS
                else:
                    print("No pipeline runs found — using sample eval data")
                    run, results = SAMPLE_RUN, SAMPLE_RESULTS

                esc_rows = await conn.fetch(
                    """SELECT thread_id, subject, body, queue, priority, email_type,
                              escalated_agents, summaries, draft_replies,
                              status, human_decision, created_at, decided_at
                       FROM escalation_queue ORDER BY created_at DESC"""
                )
                escalations = [dict(r) for r in esc_rows] if esc_rows else SAMPLE_ESCALATIONS
                if not esc_rows:
                    print("No escalations found — using sample HITL data")

            await pool.close()
        except Exception as exc:
            print(f"DB error ({exc}) — using sample data")
            run, results, escalations = SAMPLE_RUN, SAMPLE_RESULTS, SAMPLE_ESCALATIONS

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_template(run, results, escalations), encoding="utf-8")
    print(f"Wrote showcase → {OUT}")
    print(f"Open: open {OUT}")


if __name__ == "__main__":
    asyncio.run(main())

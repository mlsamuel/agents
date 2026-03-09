"""
export_showcase.py — Generate a self-contained static showcase HTML file.

Queries the pipeline_runs / pipeline_results tables and bakes everything
(data, CSS, JS) into a single index.html that can be opened in any browser
without a server.

Usage:
    cd agent-cli
    python ui/export_showcase.py            # latest run
    python ui/export_showcase.py --run 3   # specific run id
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

OUT = Path(__file__).parent / "showcase" / "index.html"


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

.app-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 24px; border-bottom: 1px solid var(--border);
  background: var(--surface); position: sticky; top: 0; z-index: 10;
}
.header-left h1 { font-size: 17px; font-weight: 600; }
.header-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; display: block; }
.run-stats { display: flex; gap: 16px; font-size: 13px; color: var(--text-muted); }
.run-stats span { display: flex; align-items: center; gap: 4px; }

.app-main {
  padding: 20px 24px; max-width: 1200px; margin: 0 auto;
  width: 100%; display: flex; flex-direction: column; gap: 10px;
}

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
.result-index { font-size: 12px; color: var(--text-muted); min-width: 28px; }
.result-subject { font-weight: 500; font-size: 14px; flex: 1; }

.avg-badge {
  font-size: 13px; font-weight: 600; padding: 2px 8px; border-radius: 12px;
}
.score-green { color: var(--green); background: var(--green-bg); }
.score-amber { color: var(--amber); background: var(--amber-bg); }
.score-red   { color: var(--red);   background: var(--red-bg); }

.result-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.tag {
  font-size: 11px; padding: 2px 7px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 4px; color: var(--text-muted);
}
.tag.skill {
  color: var(--accent); border-color: rgba(67,97,238,0.3);
  background: rgba(67,97,238,0.08);
}

.result-scores { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.score-pill {
  font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 500;
}
.score-comment { font-size: 12px; color: var(--text-muted); font-style: italic; }

.expand-toggle {
  position: absolute; right: 16px; top: 16px;
  font-size: 11px; color: var(--text-muted);
}

.result-body {
  border-top: 1px solid var(--border); padding: 16px;
  display: none; flex-direction: column; gap: 12px;
}
.result-body.open { display: flex; }

.reply-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.reply-col {
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 8px; overflow: hidden;
}
.reply-col-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-muted);
  padding: 8px 12px; border-bottom: 1px solid var(--border);
}
.reply-text {
  padding: 12px; font-family: inherit; font-size: 13px;
  line-height: 1.6; white-space: pre-wrap; word-break: break-word;
}
.tools-line { font-size: 12px; color: var(--text-muted); }

.generated-at {
  text-align: center; padding: 32px; font-size: 12px; color: var(--text-muted);
}
"""


JS = """
function scoreClass(v) {
  if (v == null) return '';
  if (v <= 2) return 'score-red';
  if (v === 3) return 'score-amber';
  return 'score-green';
}

function scorePill(label, value) {
  const cls = scoreClass(value);
  return `<span class="score-pill ${cls}">${label} ${value ?? '—'}/5</span>`;
}

function fmtDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit'
  });
}

function buildCard(r, index) {
  const avgCls = scoreClass(r.score_avg ? Math.round(r.score_avg) : null);
  const avgStr = r.score_avg != null ? r.score_avg.toFixed(1) + ' / 5' : '—';
  const skillTag = r.skills ? `<span class="tag skill">${r.skills}</span>` : '';
  const comment = (r.score_comment && r.score_comment !== 'none')
    ? `<span class="score-comment">"${r.score_comment}"</span>` : '';
  const toolsLine = r.tools
    ? `<p class="tools-line">Tools used: ${r.tools}</p>` : '';

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
      ${skillTag}
    </div>
    <div class="result-scores">
      ${scorePill('action', r.score_action)}
      ${scorePill('completeness', r.score_completeness)}
      ${scorePill('tone', r.score_tone)}
      ${comment}
    </div>
    <span class="expand-toggle">▼</span>
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
    ${toolsLine}
  </div>
</div>`;
}

function esc(s) {
  return (s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggle(header) {
  const body = header.nextElementSibling;
  const arrow = header.querySelector('.expand-toggle');
  body.classList.toggle('open');
  arrow.textContent = body.classList.contains('open') ? '▲' : '▼';
}

window.toggle = toggle;

(function () {
  const run = DATA.run;
  const results = DATA.results;

  const header = document.getElementById('header');
  const main = document.getElementById('main');

  header.innerHTML = `
    <div class="header-left">
      <h1>agent-cli showcase</h1>
      <span class="header-sub">Claude · CLI transport · eval results</span>
    </div>
    <div class="run-stats">
      <span>${fmtDate(run.run_at)}</span>
      <span>${run.total ?? results.length} emails</span>
      ${run.avg_overall != null ? `<span>avg <strong>${run.avg_overall.toFixed(1)}/5</strong></span>` : ''}
    </div>`;

  main.innerHTML = results.map(buildCard).join('') +
    `<p class="generated-at">Generated ${new Date().toLocaleString()}</p>`;
})();
"""


def html_template(run: dict, results: list[dict]) -> str:
    data_js = json.dumps({"run": run, "results": results}, default=str, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>agent-cli showcase</title>
  <style>{CSS}</style>
</head>
<body>
<div class="app">
  <header class="app-header" id="header"></header>
  <main class="app-main" id="main"></main>
</div>
<script>const DATA = {data_js};</script>
<script>{JS}</script>
</body>
</html>"""


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=int, default=None, help="Run ID (default: latest)")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")

    pool = await asyncpg.create_pool(url)
    async with pool.acquire() as conn:
        if args.run:
            run_row = await conn.fetchrow("SELECT * FROM pipeline_runs WHERE id = $1", args.run)
            if not run_row:
                sys.exit(f"Run {args.run} not found")
        else:
            run_row = await conn.fetchrow(
                "SELECT * FROM pipeline_runs ORDER BY run_at DESC LIMIT 1"
            )
            if not run_row:
                sys.exit("No pipeline runs found. Run the pipeline first:\n  python pipeline.py --limit 20")

        result_rows = await conn.fetch(
            """SELECT email_index, subject, body, queue, email_type, priority,
                      skills, tools, ground_truth, generated,
                      score_action, score_completeness, score_tone, score_avg, score_comment
               FROM pipeline_results WHERE run_id = $1 ORDER BY email_index""",
            run_row["id"],
        )

    await pool.close()

    run = dict(run_row)
    results = [dict(r) for r in result_rows]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_template(run, results), encoding="utf-8")

    print(f"Wrote {len(results)} results → {OUT}")
    print(f"Open with:  open {OUT}")


if __name__ == "__main__":
    asyncio.run(main())

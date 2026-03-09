# agent-cli showcase UI

Side-by-side comparison of ground-truth vs generated replies with eval scores, browseable by pipeline run.

## Setup

**Backend** (Python, reads from Postgres):

```bash
cd ui/backend
pip install -r requirements.txt
uvicorn main:app --port 8000 --reload
```

**Frontend** (React + Vite):

```bash
cd ui/frontend
npm install
npm run dev
```

Open http://localhost:5173

## Populating data

Run the pipeline first — results are stored automatically on every eval run:

```bash
cd ..   # agent-cli/
python pipeline.py --limit 20 --no-improve
```

Each run creates a row in `pipeline_runs` and one row per email in `pipeline_results`. The UI shows the most recent run by default, with a dropdown to switch between runs.

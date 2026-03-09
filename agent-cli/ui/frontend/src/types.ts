export interface Run {
  id: number;
  run_at: string;
  limit_: number | null;
  offset_: number | null;
  language: string | null;
  total: number | null;
  avg_action: number | null;
  avg_completeness: number | null;
  avg_tone: number | null;
  avg_overall: number | null;
}

export interface Result {
  id: number;
  email_index: number;
  subject: string;
  body: string;
  queue: string;
  email_type: string;
  priority: string;
  skills: string | null;
  tools: string | null;
  ground_truth: string;
  generated: string;
  internal_summary: string | null;
  score_action: number | null;
  score_completeness: number | null;
  score_tone: number | null;
  score_avg: number | null;
  score_comment: string | null;
}

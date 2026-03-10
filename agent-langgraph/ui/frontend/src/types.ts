export interface Escalation {
  thread_id: string;
  subject: string;
  body: string | null;
  queue: string | null;
  priority: string | null;
  email_type: string | null;
  escalated_agents: string[];
  summaries: string[];
  draft_replies: string[];
  status: "pending" | "decided" | "approved" | "overridden";
  human_decision: string | null;
  created_at: string;
  decided_at: string | null;
}

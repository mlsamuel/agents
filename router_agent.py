"""
router_agent.py - Routes a classified email to the correct WorkflowAgent.

Queue → agent_key mapping. Multiple queues can share an agent (e.g. IT Support
and Technical Support both route to the technical_support skill folder).
"""

from workflow_agent import WorkflowAgent, WorkflowResult

QUEUE_MAP: dict[str, str] = {
    # ── Specialist queues ──────────────────────────────────────────────────────
    "Technical Support":                        "technical_support",
    "IT Support":                               "technical_support",
    "Product Support":                          "technical_support",
    "Service Outages and Maintenance":          "technical_support",
    "Billing and Payments":                     "billing",
    "Returns and Exchanges":                    "returns",
    "Customer Service":                         "general",
    "Sales and Pre-Sales":                      "general",
    "General Inquiry":                          "general",
    "Human Resources":                          "general",
    # ── General topic queues (all → general handler) ──────────────────────────
    "Arts & Entertainment/Movies":              "general",
    "Arts & Entertainment/Music":               "general",
    "Autos & Vehicles/Maintenance":             "general",
    "Autos & Vehicles/Sales":                   "general",
    "Beauty & Fitness/Cosmetics":               "general",
    "Beauty & Fitness/Fitness Training":        "general",
    "Books & Literature/Fiction":               "general",
    "Books & Literature/Non-Fiction":           "general",
    "Business & Industrial/Manufacturing":      "general",
    "Finance/Investments":                      "general",
    "Finance/Personal Finance":                 "general",
    "Food & Drink/Groceries":                   "general",
    "Food & Drink/Restaurants":                 "general",
    "Games":                                    "general",
    "Health/Medical Services":                  "general",
    "Health/Mental Health":                     "general",
    "Hobbies & Leisure/Collectibles":           "general",
    "Hobbies & Leisure/Crafts":                 "general",
    "Home & Garden/Home Improvement":           "general",
    "Home & Garden/Landscaping":                "general",
    "IT & Technology/Hardware Support":         "technical_support",
    "IT & Technology/Network Infrastructure":   "technical_support",
    "IT & Technology/Security Operations":      "technical_support",
    "IT & Technology/Software Development":     "technical_support",
    "Jobs & Education/Online Courses":          "general",
    "Jobs & Education/Recruitment":             "general",
    "Law & Government/Government Services":     "general",
    "Law & Government/Legal Advice":            "general",
    "News":                                     "general",
    "Online Communities/Forums":                "general",
    "Online Communities/Social Networks":       "general",
    "People & Society/Culture & Society":       "general",
    "Pets & Animals/Pet Services":              "general",
    "Pets & Animals/Veterinary Care":           "general",
    "Real Estate":                              "general",
    "Science/Environmental Science":            "general",
    "Science/Research":                         "general",
    "Shopping/E-commerce":                      "general",
    "Shopping/Retail Stores":                   "general",
    "Sports":                                   "general",
    "Travel & Transportation/Air Travel":       "general",
    "Travel & Transportation/Land Travel":      "general",
}


def route(classification: dict, email: dict) -> WorkflowResult:
    """Dispatch a classified email to the appropriate workflow agent."""
    queue = classification.get("queue", "")
    agent_key = QUEUE_MAP.get(queue, "general")
    agent = WorkflowAgent(agent_key)
    return agent.run(email, classification)

"""
skills.py - File-based skill loading and versioning.

Skills are Markdown files with YAML frontmatter stored under data/skills/<agent>/<name>.md.
When a skill is improved, the current file is renamed to <name>.v<n>.bak and a new
<name>.md is written. Rollback restores the most recent .bak file.

Public API:
    load_skills(agent_key)                        -> dict[str, dict]
    select_skill(skills, email_type, subject)     -> str  (system prompt content)
    upsert_version(skill_path, new_content)       -> int  (new version number)
    rollback(agent_key, skill_name)               -> bool
    parse_frontmatter(text)                       -> (dict, str)
    all_skills()                                  -> dict[str, dict]  (all agents)
"""

import re
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).parent / "data" / "skills"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter and body. Returns ({}, text) if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


def load_skills(agent_key: str) -> dict[str, dict]:
    """Return all active skills for an agent as {name: {agent, types, tools, content}}.

    Falls back to 'general' if the agent directory has no skills.
    """
    agent_dir = SKILLS_DIR / agent_key
    if not agent_dir.exists():
        agent_dir = SKILLS_DIR / "general"
    if not agent_dir.exists():
        return {}

    result: dict[str, dict] = {}
    for md in sorted(agent_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        name = meta.get("name", md.stem)
        result[name] = {
            "agent":   meta.get("agent", agent_key),
            "types":   meta.get("types", []),
            "tools":   meta.get("tools", []),
            "content": body,
            "path":    str(md),
        }
    return result


def all_skills() -> dict[str, dict]:
    """Return all active skills across all agents as {name: {agent, types, tools, content, path}}."""
    result: dict[str, dict] = {}
    if not SKILLS_DIR.exists():
        return result
    for md in sorted(SKILLS_DIR.glob("**/*.md")):
        text = md.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        name = meta.get("name", md.stem)
        result[name] = {
            "agent":   meta.get("agent", md.parent.name),
            "types":   meta.get("types", []),
            "tools":   meta.get("tools", []),
            "content": body,
            "path":    str(md),
        }
    return result


def select_skill(skills: dict[str, dict], email_type: str, subject: str) -> tuple[str, str]:
    """Pick the best skill for this email type and return (skill_name, system_prompt_content).

    If only one skill exists, returns it directly.
    If multiple skills exist, picks the one whose types list includes the email_type.
    Falls back to the first skill if no type match found.
    """
    if not skills:
        return "general_inquiry", ""

    if len(skills) == 1:
        name = next(iter(skills))
        return name, skills[name]["content"]

    # Try to match on email_type
    for name, info in skills.items():
        if email_type in info.get("types", []):
            return name, info["content"]

    # Fallback: first skill
    name = next(iter(skills))
    return name, skills[name]["content"]


def _current_version(skill_path: Path) -> int:
    """Infer current version from existing .bak files."""
    baks = list(skill_path.parent.glob(f"{skill_path.stem}.v*.bak"))
    if not baks:
        return 1
    versions = []
    for b in baks:
        m = re.search(r"\.v(\d+)\.bak$", b.name)
        if m:
            versions.append(int(m.group(1)))
    return max(versions) + 1 if versions else 2


def upsert_version(skill_path_str: str, new_content: str) -> int:
    """Write a new version of a skill file.

    Renames the current .md to <name>.v<n>.bak, then writes new_content as <name>.md.
    Returns the new version number.
    """
    skill_path = Path(skill_path_str)
    new_ver = _current_version(skill_path)

    # Back up current version
    if skill_path.exists():
        bak = skill_path.with_suffix(f".v{new_ver - 1}.bak")
        skill_path.rename(bak)

    # Write new version
    skill_path.write_text(new_content, encoding="utf-8")
    return new_ver


def rollback(agent_key: str, skill_name: str) -> bool:
    """Restore the most recent backup for a skill.

    Returns True if a backup was found and restored, False otherwise.
    """
    skill_path = SKILLS_DIR / agent_key / f"{skill_name}.md"
    baks = list(skill_path.parent.glob(f"{skill_name}.v*.bak"))
    if not baks:
        return False

    # Find highest version backup
    best = max(baks, key=lambda b: int(re.search(r"\.v(\d+)\.bak$", b.name).group(1)))

    # Remove current (bad) version
    if skill_path.exists():
        skill_path.unlink()

    # Restore backup
    best.rename(skill_path)
    return True

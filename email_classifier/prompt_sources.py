"""Deriving a fingerprint's legs from what an agent itself declares (STU-1458).

Four modules each keep a list of files that shape an LLM prompt, keyed to fingerprint a
cache or a frozen record. Every one of them repeated the same two facts by hand: which
skills and tools an agent reads are already written once, in the agent's own YAML — so
listing them again in Python is a second copy that drifts, and a skill a deployment adds
to an agent's file was invisible here until somebody remembered to add a matching line.

Skills and tools are therefore read off the agent, never re-listed: `skill_paths` and
`tool_paths` open the agent's own `.studio/agents/<name>.agent.yaml` and resolve its
declared names by the one naming convention the repo already uses everywhere else —
`skills/<name>.skill.md`, and a tool's file is its declared name's plugin half (before
the first `-`, underscores turned to dashes) plus `.tool.yaml`. Neither guesses: a name
the convention cannot resolve is a bug in the declaration, not something to paper over.

An agent, a contract or a pipeline is not declared *by* anything else, so those stay
named explicitly by their caller — this module only turns an agent's own list into
paths, it does not invent a registry for files nothing points at.
"""

from pathlib import Path

import yaml


def _agent_doc(studio_dir: Path, agent_name: str) -> dict:
    """The agent's own YAML, or an empty declaration for one that does not exist or
    does not parse.

    Neither is a raise. A missing agent is reported by the caller's own `agent` leg,
    which reads this same file and digests it `absent` (`fingerprint()`) — this
    function failing too would only be a second, worse-worded version of the same
    finding, and it would break a synthetic `.studio/` a test builds without a real
    agent file at all. Unparseable YAML follows `batch._source_digest`'s rule for the
    same reason: this function's job is to never under-cover, and a broken file is the
    loader's error to report — until it does, the safest answer is no skill or tool
    resolved from it, while the `agent` leg still hashes its raw bytes and busts the
    cache on the file having changed at all.
    """
    path = studio_dir / "agents" / f"{agent_name}.agent.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def skill_paths(studio_dir: Path, agent_name: str) -> dict[str, str]:
    """Role -> path, relative to `studio_dir`, for every skill the agent declares."""
    doc = _agent_doc(studio_dir, agent_name)
    return {
        f"skill:{skill}": f"skills/{skill}.skill.md"
        for skill in doc.get("skills", [])
    }


def tool_paths(studio_dir: Path, agent_name: str) -> dict[str, str]:
    """Role -> path, relative to `studio_dir`, for every tool plugin the agent declares.

    A tool's declared name is `<plugin>-<function>`; the plugin's own file is named
    after the plugin half with `_` turned to `-` (`org_web-search_site` ->
    `org-web.tool.yaml`), the same split the engine itself makes between the two.
    """
    doc = _agent_doc(studio_dir, agent_name)
    paths: dict[str, str] = {}
    for tool in doc.get("tools", []):
        plugin = tool.split("-", 1)[0].replace("_", "-")
        paths[f"tool:{plugin}"] = f"tools/{plugin}.tool.yaml"
    return paths

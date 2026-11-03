"""`skill_paths`/`tool_paths`, and the checkpoint STU-1458 asks for: a second
deployment declaring different files under the same roles resolves to those files,
not the first deployment's (STU-1458)."""

from pathlib import Path

from email_classifier import deployment, prompt_sources

AGENT_YAML = """\
name: {name}
model: claude-opus-4-8
skills:
{skills}
tools:
{tools}
"""


def _synthetic_studio(tmp_path: Path, agent_name: str, skills: list[str], tools: list[str]) -> Path:
    """A second deployment's `.studio/`, with its own agent declaring its own names —
    the minimal stand-in for a full reference deployment (STU-1459's job), just enough
    to prove role resolution is not hard-coded to one deployment's files."""
    studio_dir = tmp_path / "studio"
    (studio_dir / "agents").mkdir(parents=True)
    (studio_dir / "agents" / f"{agent_name}.agent.yaml").write_text(
        AGENT_YAML.format(
            name=agent_name,
            skills="\n".join(f"  - {s}" for s in skills),
            tools="\n".join(f"  - {t}" for t in tools),
        ),
        encoding="utf-8",
    )
    return studio_dir


def test_skill_paths_resolves_a_deployments_own_skill_not_anothers(tmp_path):
    studio_dir = _synthetic_studio(tmp_path, "greeter", ["welcome-pack"], [])
    assert prompt_sources.skill_paths(studio_dir, "greeter") == {
        "skill:welcome-pack": "skills/welcome-pack.skill.md"
    }


def test_tool_paths_resolves_a_deployments_own_tool_not_anothers(tmp_path):
    studio_dir = _synthetic_studio(tmp_path, "greeter", [], ["roster_lookup-find_member"])
    assert prompt_sources.tool_paths(studio_dir, "greeter") == {
        "tool:roster-lookup": "tools/roster-lookup.tool.yaml"
    }


def test_two_deployments_resolve_the_same_roles_to_different_files(tmp_path):
    """The checkpoint STU-1458 exists for: two deployments, same role vocabulary
    (`skill:*`), different files behind it — nothing here names a deployment by path."""
    active_studio = deployment.ROOT / ".studio"
    other_studio = _synthetic_studio(tmp_path, "classifier", ["local-taxonomy"], [])

    active_skills = prompt_sources.skill_paths(active_studio, "classifier")
    other_skills = prompt_sources.skill_paths(other_studio, "classifier")

    assert active_skills == {"skill:taxonomy": "skills/taxonomy.skill.md"}
    assert other_skills == {"skill:local-taxonomy": "skills/local-taxonomy.skill.md"}
    assert active_skills != other_skills


def test_an_agent_with_no_declaration_resolves_to_nothing():
    assert prompt_sources.skill_paths(Path("/nonexistent"), "ghost") == {}
    assert prompt_sources.tool_paths(Path("/nonexistent"), "ghost") == {}

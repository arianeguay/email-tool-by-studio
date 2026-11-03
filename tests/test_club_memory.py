"""The club memory store (STU-873). Everything is a tmp_path — no repo directory."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from email_classifier import club_memory

# `for_prompt` tokenizes, and the tokenizer needs the `ner` extra to find a name.
# The tests below assert on that redaction, so they skip rather than stub it.
from tests.conftest import requires_gliner
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = deployment.ROOT / ".studio/tools/org-memory.tool.yaml"


def _remember(directory, name, description="d", source="PERSON_1", fact="f", **kwargs):
    return club_memory.remember(directory, name, description, source, fact, **kwargs)


def test_a_memory_round_trips_its_source_and_date(tmp_path):
    _remember(tmp_path, "u15-practice-gym", description="U15 practise at Leblanc",
              source="PERSON_1 <EMAIL_2>", fact="The U15 girls practise at école Leblanc.",
              learned_on="2026-08-20")

    front, body = club_memory._parse(tmp_path / "u15-practice-gym.md")
    assert front["source"] == "PERSON_1 <EMAIL_2>"
    assert front["learned_on"] == "2026-08-20"
    assert body == "The U15 girls practise at école Leblanc."


def test_the_index_is_one_line_per_memory(tmp_path):
    for name in ("fee-2026", "u15-practice-gym", "tournament-date"):
        _remember(tmp_path, name)

    index = club_memory.load_index(tmp_path)
    files = [p for p in tmp_path.glob("*.md") if p.name != club_memory.INDEX_NAME]
    assert len(index.splitlines()) == len(files) == 3
    assert all(line.startswith("- [") for line in index.splitlines())


def test_the_index_line_carries_the_date_so_a_stale_memory_is_visible(tmp_path):
    _remember(tmp_path, "fee-2026", description="Registration is 285$",
              source="PERSON_3", learned_on="2026-02-01")

    line = club_memory.load_index(tmp_path).strip()
    assert "Registration is 285$" in line
    assert "learned 2026-02-01 from PERSON_3" in line
    assert "(fee-2026.md)" in line


def test_rewriting_a_name_replaces_the_memory_rather_than_adding_one(tmp_path):
    _remember(tmp_path, "fee-2026", description="285$")
    _remember(tmp_path, "fee-2026", description="290$")

    assert len(club_memory.entries(tmp_path)) == 1
    assert "290$" in club_memory.load_index(tmp_path)


def test_a_contradicted_memory_is_flagged_and_kept(tmp_path):
    _remember(tmp_path, "fee-2026", description="Registration is 285$")
    club_memory.flag_contradicted(tmp_path, "fee-2026", "the site says 290$")

    assert (tmp_path / "fee-2026.md").exists()
    front, _ = club_memory._parse(tmp_path / "fee-2026.md")
    assert front["contradicted"] == "the site says 290$"

    line = club_memory.load_index(tmp_path).strip()
    assert line.startswith("- [contradicted] ")
    assert "contradicted: the site says 290$" in line


def test_an_index_that_was_never_written_is_empty_not_an_error(tmp_path):
    assert club_memory.load_index(tmp_path / "nothing-here") == ""


@pytest.mark.parametrize("name", ["../escape", "Fee 2026", "a/b", ""])
def test_a_name_that_is_not_a_slug_is_refused(tmp_path, name):
    """The agent names the memory, so the name is also the path it would write to."""
    with pytest.raises(ValueError):
        _remember(tmp_path, name)


@requires_gliner
def test_a_memory_is_redacted_against_the_batch_keymap_when_it_is_read(tmp_path):
    """A hand-edited value is caught; a token the drafter wrote is not (STU-879)."""
    keymap = {"EMAIL_1": "parent@example.ca", "PERSON_1": "Marie Tremblay"}

    out = club_memory.for_prompt(
        "PERSON_1 confirms. Mme Isabelle Bergeron <parent@example.ca> too.", keymap)

    assert out.startswith("PERSON_1 confirms.")
    assert "EMAIL_1" in out
    assert "Isabelle Bergeron" not in out
    assert "parent@example.ca" not in out


@requires_gliner
def test_a_token_the_drafter_wrote_reaches_the_prompt_unchanged(tmp_path):
    """STU-981: re-tokenizing an already-tokenized memory re-minted its source token,
    and the keymap did not know the new one — so the fact came back from nobody."""
    memory = ("---\nname: fee-2026\nsource: PERSON_53\n---\n\n"
              "Le montant peut être donné tel quel aux parents, sans détail.")

    out = club_memory.for_prompt(memory, {"PERSON_53": "Nathalie Roy"})

    assert "source: PERSON_53" in out
    assert "aux parents," in out


@requires_gliner
def test_a_value_the_keymap_does_not_know_gets_an_index_free_placeholder(tmp_path):
    """An indexed token would be restored as the batch's PERSON_2 — someone else."""
    out = club_memory.for_prompt("Mme Isabelle Bergeron confirms.",
                                 {"PERSON_1": "Marie Tremblay"})

    # The honorific rides inside the span, so restoration hands the title back too.
    assert out == "PERSON_X confirms."


def _tool(tmp_path, command, payload, check=True, keymap=None):
    return subprocess.run(
        [sys.executable, "-m", "email_classifier.club_memory",
         "--directory", str(tmp_path), *(["--keymap", str(keymap)] if keymap else []),
         command],
        input=json.dumps(payload), capture_output=True, text=True, check=check,
    )


@requires_gliner
def test_the_tool_writes_then_reads_back_the_fact(tmp_path):
    written = _tool(tmp_path, "remember", {
        "name": "gym-assignment", "description": "U15 gym", "source": "PERSON_1",
        "fact": "The U15 girls practise at école Leblanc.",
    })
    assert "gym-assignment" in json.loads(written.stdout)["index"]

    read = _tool(tmp_path, "read", {"name": "gym-assignment"})
    # The subject as well as the school: asserting on `école Leblanc` alone let the
    # fact read back as "PERSON_X practise at école Leblanc." and still pass (STU-986).
    assert json.loads(read.stdout)["content"].count(
        "The U15 girls practise at école Leblanc."
    ) == 1


@requires_gliner
def test_the_tool_redacts_the_memory_it_reads_back(tmp_path):
    """The index is not the only way in: a file is a tool call away (STU-879)."""
    keymap = tmp_path / "keymap.json"
    keymap.write_text(json.dumps({"EMAIL_1": "parent@example.ca"}), encoding="utf-8")
    _tool(tmp_path, "remember", {
        "name": "gym-assignment", "description": "U15 gym",
        "source": "Mme Isabelle Bergeron <parent@example.ca>",
        "fact": "The U15 girls practise at école Leblanc.",
    })

    read = _tool(tmp_path, "read", {"name": "gym-assignment"}, keymap=keymap)

    content = json.loads(read.stdout)["content"]
    assert "Isabelle Bergeron" not in content
    assert "parent@example.ca" not in content
    assert "EMAIL_1" in content
    assert "école Leblanc" in content


@requires_gliner
def test_the_tool_keeps_prose_the_shell_would_have_eaten(tmp_path):
    """The agent's words reach Python through stdin, not through a shell argument."""
    hostile = 'Le "gymnase" coûte 285$ — $PATH, `date`'
    _tool(tmp_path, "remember", {
        "name": "fee-2026", "description": hostile, "source": "PERSON_1",
        "fact": hostile,
    })

    assert hostile in json.loads(_tool(tmp_path, "read", {"name": "fee-2026"}).stdout)["content"]


def _studio(relative):
    return yaml.safe_load((deployment.ROOT / ".studio" / relative).read_text(encoding="utf-8"))


def _render(command_name, params):
    """The shell Studio would run: `{{x}}` and `{{x | json}}` into the real plugin."""
    plugin = yaml.safe_load(TOOL.read_text(encoding="utf-8"))
    command = next(c for c in plugin["commands"] if c["name"] == command_name)
    return re.sub(r"\{\{(\w+)(\s*\|\s*json)?\}\}",
                  lambda m: json.dumps(params[m[1]]) if m[2] else params[m[1]],
                  command["execute"]["command"])


def _shell(rendered, memory_dir):
    """PYTHONPATH so a worktree renders its own sources, not the editable install."""
    return subprocess.run(
        ["sh", "-c", rendered], cwd=REPO_ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHON": sys.executable,
             "PYTHONPATH": str(REPO_ROOT), "CLUB_MEMORY_DIR": str(memory_dir),
             "EMAIL_CLASSIFIER_DEPLOYMENT_ROOT": str(deployment.ROOT)},
    )


def test_the_plugin_command_runs_as_studio_would_render_it(tmp_path):
    """The heredoc and the JSON payload are the wiring most likely to be wrong."""
    written = _shell(_render("org_memory-remember", {
        "name": "gym-assignment",
        "description": 'Le "gymnase" des U15',
        "source": "PERSON_1",
        "fact": "Les U15 pratiquent à l'école Leblanc — 285$ la saison.",
    }), tmp_path)

    assert written.returncode == 0, written.stderr
    assert 'Le "gymnase" des U15' in json.loads(written.stdout)["index"]
    assert "285$" in club_memory.read(tmp_path, "gym-assignment")


@requires_gliner
def test_the_read_command_takes_the_keymap_from_the_run_not_the_agent(tmp_path):
    """The agent chooses which memory to open, never what it is redacted against."""
    keymap = tmp_path / "keymap.json"
    keymap.write_text(json.dumps({"EMAIL_1": "parent@example.ca"}), encoding="utf-8")
    _remember(tmp_path, "gym-assignment", source="Mme Isabelle Bergeron",
              fact="Les U15 pratiquent à l'école Leblanc — parent@example.ca.")

    read = _shell(_render("org_memory-read_memory",
                          {"name": "gym-assignment", "keymap": str(keymap)}), tmp_path)

    assert read.returncode == 0, read.stderr
    content = json.loads(read.stdout)["content"]
    assert "Isabelle Bergeron" not in content
    assert "EMAIL_1" in content
    assert _parameters("org_memory-read_memory")["keymap"]["from_context"] == "input.keymap"


def _parameters(command_name):
    plugin = yaml.safe_load(TOOL.read_text(encoding="utf-8"))
    return next(c for c in plugin["commands"] if c["name"] == command_name)["parameters"]


def test_the_tool_refuses_an_empty_memory(tmp_path):
    result = _tool(tmp_path, "remember", {
        "name": "nothing", "description": "d", "source": "s", "fact": "   ",
    }, check=False)

    assert result.returncode != 0
    assert not (tmp_path / "nothing.md").exists()


def _unwrapped(text):
    """Prompts are hand-wrapped, so a phrase to assert on straddles a newline."""
    return " ".join(text.split())


def _prompt_surfaces():
    """Every prompt a drafting run reads the precedence rule from (STU-980)."""
    return {
        "drafter": _studio("agents/drafter.agent.yaml")["system_prompt"],
        "draft-reviewer": _studio("agents/draft-reviewer.agent.yaml")["system_prompt"],
        "club_memory": yaml.safe_load(TOOL.read_text(encoding="utf-8"))["prompt_snippet"],
    }


# The two agent surfaces are the first deployment's own prose and stay with it.
@pytest.mark.parametrize("surface", ["club_memory"])
def test_a_surface_stating_precedence_also_names_silence_apart(surface):
    """STU-980: `club:fees` says the club has not published an amount, and that read
    as a fact a memory could contradict — so the memory holding the amount was
    flagged, and every later run fell back to "we will confirm"."""
    prompt = _unwrapped(_prompt_surfaces()[surface])

    assert "outrank" in prompt, "the surface no longer states precedence"
    assert "silence" in prompt.lower(), "precedence stated without naming silence apart"
    assert "fallback" in prompt


def _reject_gates():
    """The reviewer's reject list, one unwrapped bullet per gate."""
    prompt = _studio("agents/draft-reviewer.agent.yaml")["system_prompt"]
    body = prompt.split("Reject when any of these holds:")[1].split("Approve a draft")[0]
    return [_unwrapped(gate) for gate in body.split("\n- ")[1:]]


def test_the_flag_tool_refuses_a_gap_as_a_reason():
    """The rule has to reach the call site: the agent reads this, not the snippet."""
    flag = next(c for c in yaml.safe_load(TOOL.read_text(encoding="utf-8"))["commands"]
                if c["name"] == "org_memory-flag_contradicted")

    assert "answers differently" in _unwrapped(flag["description"])
    assert "not a reason to flag" in _unwrapped(
        flag["parameters"]["reason"]["description"])

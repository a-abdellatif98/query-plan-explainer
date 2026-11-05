import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
DOCS = [p for p in ROOT.rglob("*.md") if ".pytest_cache" not in p.parts]


def frontmatter(path):
    return path.read_text().split("---")[1]


def test_plugin_ships_no_command_hooks():
    hooks_file = ROOT / "hooks" / "hooks.json"
    if hooks_file.exists():
        config = json.loads(hooks_file.read_text())
        hooks = [h for groups in config["hooks"].values() for group in groups for h in group["hooks"]]
        assert all(h["type"] == "prompt" for h in hooks)


def test_no_preapproved_shell_access():
    for path in list((ROOT / "commands").glob("*.md")) + list((ROOT / "agents").glob("*.md")):
        front = frontmatter(path)
        assert "Bash" not in front, path.name
        tools = re.search(r"(?:allowed-tools|tools):\s*\[?([^\]\n]*)", front).group(1)
        assert {t.strip() for t in tools.split(",")} <= {"Read", "Glob", "Grep"}, path.name


def test_command_receives_arguments_and_avoids_secrets():
    text = (ROOT / "commands" / "explain-plan.md").read_text()
    assert "$ARGUMENTS" in text
    assert "argument-hint:" in frontmatter(ROOT / "commands" / "explain-plan.md")
    assert "Never open `.env`" in text


def test_manifest_has_required_fields():
    for key in ("name", "description", "version", "author", "homepage", "repository", "license", "keywords", "icon"):
        assert MANIFEST.get(key), key
    assert MANIFEST["name"] == "query-plan-explainer"
    assert MANIFEST["repository"] == "https://github.com/a-abdellatif98/query-plan-explainer"
    assert (ROOT / MANIFEST["icon"]).is_file()


def test_manifest_version_matches_skill():
    skill = (ROOT / "skills" / "explain-plan" / "SKILL.md").read_text()
    assert f"version: {MANIFEST['version']}" in skill
    assert "name: explain-plan" in skill


def test_skill_references_exist():
    skill = (ROOT / "skills" / "explain-plan" / "SKILL.md").read_text()
    for name in re.findall(r"references/([\w-]+\.md)", skill):
        assert (ROOT / "skills" / "explain-plan" / "references" / name).is_file(), name


def test_no_root_claude_md():
    assert not (ROOT / "CLAUDE.md").exists()


def test_docs_have_no_em_or_en_dashes():
    for path in DOCS:
        assert "\u2014" not in path.read_text() and "\u2013" not in path.read_text(), path.name


def test_skill_fix_sections_exist_in_fixes_reference():
    skill_dir = ROOT / "skills" / "explain-plan"
    fixes = (skill_dir / "references" / "fixes.md").read_text()
    headings = set(re.findall(r"^## (.+)$", fixes, re.MULTILINE))
    cited = set(re.findall(r'fixes\.md "([^"]+)"', (skill_dir / "SKILL.md").read_text()))
    assert cited
    assert cited <= headings, cited - headings

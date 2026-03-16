"""Tests for skill discovery/parsing helpers."""

from pathlib import Path

import coco.skills as skills


def test_parse_skill_frontmatter_extracts_name_and_description():
    text = (
        "---\n"
        "name: demo-skill\n"
        "description: \"Demo description\"\n"
        "icon: \"🧪\"\n"
        "---\n"
        "# Demo\n"
    )
    name, description, icon = skills.parse_skill_frontmatter(text)
    assert name == "demo-skill"
    assert description == "Demo description"
    assert icon == "🧪"


def test_discover_skills_prefers_first_root_for_duplicates(tmp_path: Path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    (root_a / "demo").mkdir(parents=True)
    (root_b / "demo").mkdir(parents=True)
    (root_a / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: From A\n---\n",
        encoding="utf-8",
    )
    (root_b / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: From B\n---\n",
        encoding="utf-8",
    )

    catalog = skills.discover_skills([root_a, root_b])
    assert "demo" in catalog
    assert catalog["demo"].description == "From A"
    assert catalog["demo"].source_root == root_a.resolve()


def test_resolve_skill_identifier_matches_name_or_folder_alias(tmp_path: Path):
    root = tmp_path / "skills"
    (root / "coco-delivery").mkdir(parents=True)
    (root / "coco-delivery" / "SKILL.md").write_text(
        "---\nname: coco-delivery\ndescription: CoCo delivery flow\n---\n",
        encoding="utf-8",
    )

    catalog = skills.discover_skills([root])
    assert skills.resolve_skill_identifier("coco-delivery", catalog) == "coco-delivery"
    assert skills.resolve_skill_identifier("COCO Delivery", catalog) == "coco-delivery"

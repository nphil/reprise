"""The install files agree with each other and stay review-only safe.

A fresh install must not share Plex's folder, or write into Plex, until its owner chooses to. These tests parse the
Unraid template and the Compose example and check:

* the defaults: media read-only, no Plex folder shared, the optional Plex lines optional and empty or commented out;
* that the optional Compose lines, switched on, still make a valid file with the extra folder and variable;
* that the pinned image is the same in the template, the Compose file and the README, so a pin that is updated in one
  place and forgotten in another fails here;
* that every README link the app and the install files point at lands on a heading that exists.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "unraid-templates" / "reprise.xml"
TEMPLATE_README = REPO_ROOT / "unraid-templates" / "README.md"
COMPOSE = REPO_ROOT / "docker-compose.example.yml"
README = REPO_ROOT / "README.md"
APP = REPO_ROOT / "media_preview_generator"

FENCE = re.compile(r"^ {0,3}```")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
LINK = re.compile(r"\]\(([^)\s]+)\)")
# How the app and the install files link to the README: a full URL, or the setup guide's `f"{README}#..."`.
README_LINK = re.compile(r"(?:github\.com/nphil/reprise|\{README\})#([A-Za-z0-9_-]+)")

# The README headings the app's pages and the install files point people at. Renaming one breaks those links.
APP_ANCHORS = (
    "install-and-set-up",
    "share-your-media-folders-with-reprise",
    "optional-gpu-decoding",
    "turn-on-sending-to-plex",
    "troubleshooting",
)

# The commented-out Compose lines a person switches on. Each must sit right under a "# (optional)" label.
OPTIONAL_COMPOSE_LINES = {
    "second media folder": ":/media2:ro",
    "Plex folder": ":/plex:rw",
    "Plex folder variable": "PLEX_CONFIG_FOLDER:",
}


# ── Markdown helpers ─────────────────────────────────────────────────────────────────────────────────────────


def _slug(heading: str) -> str:
    """The id GitHub gives a heading: lower case, punctuation dropped, spaces turned into hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def _outside_fences(text: str) -> str:
    """The text without its fenced code blocks, where a ``# comment`` is not a heading and a link is not a link."""
    kept, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return "\n".join(kept)


def _anchors(path: Path) -> set[str]:
    """Every heading id GitHub gives a Markdown file (a repeated heading gets -1, -2, and so on)."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    for line in _outside_fences(path.read_text(encoding="utf-8")).splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        found.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return found


def _readme_section(title: str) -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index(f"\n## {title}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


# ── The Unraid template ──────────────────────────────────────────────────────────────────────────────────────


def _template_configs(kind: str) -> dict[str, ET.Element]:
    """The template's Config fields of one type, by container path (Path) or variable name (Variable)."""
    configs = [c for c in ET.parse(TEMPLATE).getroot().findall("Config") if c.get("Type") == kind]
    by_target = {c.get("Target"): c for c in configs}
    assert len(by_target) == len(configs), f"two {kind} fields share one target"
    return by_target


def _paths() -> dict[str, ET.Element]:
    return _template_configs("Path")


def _is_empty(config: ET.Element) -> bool:
    """No value and no default. Unraid fills an empty value with the default, and skips a Path that stays empty."""
    return not (config.text or "").strip() and not (config.get("Default") or "").strip()


def test_template_is_the_reprise_container() -> None:
    root = ET.parse(TEMPLATE).getroot()
    assert root.tag == "Container"
    assert root.findtext("Name") == "Reprise"


def test_app_data_is_read_write_and_required() -> None:
    config = _paths()["/config"]
    assert (config.get("Mode"), config.get("Required")) == ("rw", "true")


def test_media_is_read_only_required_and_left_for_the_owner_to_choose() -> None:
    config = _paths()["/media"]
    assert (config.get("Mode"), config.get("Required")) == ("ro", "true")
    assert _is_empty(config)


def test_second_media_folder_is_optional_read_only_and_empty() -> None:
    config = _paths()["/media2"]
    assert (config.get("Mode"), config.get("Required"), config.get("Display")) == ("ro", "false", "advanced")
    assert _is_empty(config)


def test_plex_folder_is_optional_read_write_and_empty() -> None:
    config = _paths()["/plex"]
    assert (config.get("Mode"), config.get("Required")) == ("rw", "false")
    assert _is_empty(config)
    assert "https://github.com/nphil/reprise#turn-on-sending-to-plex" in config.get("Description", "")


def test_only_app_data_and_the_optional_plex_folder_are_writable() -> None:
    writable = {target for target, config in _paths().items() if config.get("Mode") != "ro"}
    assert writable == {"/config", "/plex"}


def test_only_app_data_has_a_host_folder_by_default() -> None:
    shared = {target for target, config in _paths().items() if not _is_empty(config)}
    assert shared == {"/config"}


def test_plex_config_folder_variable_is_optional_and_empty() -> None:
    config = _template_configs("Variable")["PLEX_CONFIG_FOLDER"]
    assert config.get("Required") == "false"
    assert _is_empty(config)


def test_overview_says_what_to_do_on_the_first_start() -> None:
    overview = ET.parse(TEMPLATE).getroot().findtext("Overview", "").lower()
    for phrase in ("create your account", "setup guide", "review only"):
        assert phrase in overview, f"the template's Overview no longer says {phrase!r}"


# ── The Compose example ──────────────────────────────────────────────────────────────────────────────────────


def _service(text: str) -> dict:
    return yaml.safe_load(text)["services"]["reprise"]


def _mounts(service: dict) -> dict[str, str]:
    """Container path to access mode for every volume line (a host path may hold spaces, never a colon)."""
    mounts = {}
    for volume in service["volumes"]:
        _host, container, mode = volume.rsplit(":", 2)
        mounts[container] = mode
    return mounts


def _optional_line_numbers(lines: list[str]) -> dict[str, int]:
    """Where each commented-out optional line is: exactly one per entry of ``OPTIONAL_COMPOSE_LINES``."""
    found = {}
    for name, token in OPTIONAL_COMPOSE_LINES.items():
        hits = [
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith("# ") and token in line and "(optional)" not in line
        ]
        assert len(hits) == 1, f"expected one commented-out {name} line, found {len(hits)}"
        found[name] = hits[0]
    return found


def _with_optional_lines_on(text: str) -> str:
    lines = text.splitlines()
    for index in _optional_line_numbers(lines).values():
        lines[index] = lines[index].replace("# ", "", 1)
    return "\n".join(lines) + "\n"


def test_compose_defaults_share_no_plex_folder() -> None:
    service = _service(COMPOSE.read_text(encoding="utf-8"))
    mounts = _mounts(service)
    assert (mounts["/config"], mounts["/media"]) == ("rw", "ro")
    assert "/plex" not in mounts
    assert "PLEX_CONFIG_FOLDER" not in service["environment"]
    assert {path for path, mode in mounts.items() if mode != "ro"} == {"/config"}


def test_compose_optional_lines_sit_under_an_optional_label() -> None:
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    for name, index in _optional_line_numbers(lines).items():
        assert "(optional)" in lines[index - 1], f"the {name} line has no '(optional)' label right above it"


def test_compose_optional_lines_make_a_valid_file_with_the_extra_folders() -> None:
    service = _service(_with_optional_lines_on(COMPOSE.read_text(encoding="utf-8")))
    mounts = _mounts(service)
    assert (mounts["/config"], mounts["/media"]) == ("rw", "ro")
    assert (mounts["/media2"], mounts["/plex"]) == ("ro", "rw")
    assert service["environment"]["PLEX_CONFIG_FOLDER"] == "/plex"


def test_working_folder_stays_in_the_template_and_the_compose_file() -> None:
    assert _template_configs("Variable")["TMP_FOLDER"].get("Default") == "/config/tmp"
    assert _service(COMPOSE.read_text(encoding="utf-8"))["environment"]["TMP_FOLDER"] == "/config/tmp"


# ── One pinned image ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_pinned_image_is_the_same_in_the_template_the_compose_file_and_the_readme() -> None:
    repository = ET.parse(TEMPLATE).getroot().findtext("Repository", "")
    assert re.fullmatch(r"ghcr\.io/nphil/reprise@sha256:[0-9a-f]{64}", repository), repository
    assert _service(COMPOSE.read_text(encoding="utf-8"))["image"] == f"${{IMAGE:-{repository}}}"
    assert repository in _readme_section("Image delivery and acceptance")


def test_the_readme_names_one_commit_for_the_examples_and_for_the_current_image() -> None:
    text = README.read_text(encoding="utf-8")
    examples = re.findall(r"The examples pin the accepted image built from commit `([0-9a-f]{40})`", text)
    current = re.findall(r"The current image, built from source `([0-9a-f]{40})`", text)
    assert len(examples) == 1, "the install section's pinned-image sentence is missing or repeated"
    assert examples == current


# ── README links ─────────────────────────────────────────────────────────────────────────────────────────────


def test_the_readme_has_the_headings_the_app_links_to() -> None:
    anchors = _anchors(README)
    assert [anchor for anchor in APP_ANCHORS if anchor not in anchors] == []


def test_every_readme_heading_the_app_and_the_install_files_link_to_exists() -> None:
    sources = [*APP.rglob("*.py"), *APP.rglob("*.js"), *APP.rglob("*.html"), TEMPLATE, COMPOSE]
    pointed: dict[str, list[str]] = {}
    for path in sources:
        for anchor in README_LINK.findall(path.read_text(encoding="utf-8", errors="ignore")):
            pointed.setdefault(anchor, []).append(path.relative_to(REPO_ROOT).as_posix())
    assert pointed, "no README links found: update README_LINK if the app now builds them another way"
    # "readme" is GitHub's own id for the whole README, not a heading.
    missing = {anchor: files for anchor, files in pointed.items() if anchor not in _anchors(README) | {"readme"}}
    assert missing == {}


def test_readme_links_within_the_page_land_on_headings() -> None:
    targets = {t[1:] for t in LINK.findall(_outside_fences(README.read_text(encoding="utf-8"))) if t.startswith("#")}
    assert targets, "the README no longer links within itself: this check has nothing to look at"
    assert sorted(targets - _anchors(README)) == []


def test_readme_links_to_files_land_on_files_and_headings_that_exist() -> None:
    broken = []
    for target in LINK.findall(_outside_fences(README.read_text(encoding="utf-8"))):
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        name, _, anchor = target.partition("#")
        path = REPO_ROOT / name
        if not path.exists() or (anchor and path.suffix == ".md" and anchor not in _anchors(path)):
            broken.append(target)
    assert broken == []


def test_template_readme_links_land_on_readme_headings() -> None:
    text = _outside_fences(TEMPLATE_README.read_text(encoding="utf-8"))
    targets = set(re.findall(r"\]\(\.\./README\.md#([^)\s]+)\)", text))
    assert targets, "the template README no longer links to the main README: this check has nothing to look at"
    assert sorted(targets - _anchors(README)) == []

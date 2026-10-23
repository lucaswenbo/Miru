"""Character configuration loader — parses soul.md as single source of truth.

Usage:
    from character import get_config, SOUL_PATH
    cfg = get_config()
    print(cfg.name)          # "Airi"
    print(cfg.user_address)  # "Master"
    print(cfg.hint("archetype"))  # "元気少女"
    print(cfg.raw_text)      # full soul.md content for LLM injection
"""

from __future__ import annotations

import os
import re

SOUL_PATH = os.path.join(os.path.dirname(__file__), "soul.md")

# Keep internal alias for backward compat
_SOUL_PATH = SOUL_PATH



class CharacterConfig:
    """Parsed character configuration from soul.md."""

    def __init__(self):
        # Defaults (used when soul.md is missing or unparseable)
        self.name = "Airi"
        self.user_address = "Master"
        self.personality = ""
        self.speech_patterns = ""
        self.appearance = ""
        self.backstory = ""
        self.interests = ""
        self.emotional_reactions = ""
        self.relationship_stages = ""
        self.agent_behavior = ""
        self.avatar_filename = "airi.jpeg"
        self.reference_filename = "airi_reference.webp"
        self.raw_text = ""
        self.prompt_hints: dict[str, str] = {}

    def hint(self, key: str, fallback: str = "") -> str:
        """Get a prompt hint value by key, with optional fallback."""
        return self.prompt_hints.get(key, fallback)

    def _parse(self, text):
        """Parse soul.md markdown into structured fields."""
        self.raw_text = text

        sections = {}
        current_heading = None
        current_lines = []

        for line in text.split("\n"):
            m = re.match(r'^#\s+(.+)', line)
            if m:
                if current_heading is not None:
                    sections[current_heading] = "\n".join(current_lines).strip()
                current_heading = m.group(1).strip().lower()
                current_lines = []
            else:
                current_lines.append(line)

        if current_heading is not None:
            sections[current_heading] = "\n".join(current_lines).strip()

        # Identity
        identity = sections.get("identity", "")
        name_m = re.search(r'\*\*Name\*\*:\s*(.+)', identity)
        if name_m:
            self.name = name_m.group(1).strip()
        addr_m = re.search(r'\*\*User Address\*\*:\s*(.+)', identity)
        if addr_m:
            self.user_address = addr_m.group(1).strip()

        # Simple text sections
        self.personality = sections.get("personality", "")
        self.speech_patterns = sections.get("speech patterns", "")
        self.appearance = sections.get("appearance", "")
        self.backstory = sections.get("backstory", "")
        self.interests = sections.get("interests", "")
        self.emotional_reactions = sections.get("emotional reactions", "")
        self.relationship_stages = sections.get("relationship stages", "")
        self.agent_behavior = sections.get("agent behavior", "")

        # Prompt Hints — key: value per line
        hints_raw = sections.get("prompt hints", "")
        self.prompt_hints = {}
        for line in hints_raw.split("\n"):
            hint_m = re.match(r'^-\s*(\w+):\s*(.+)', line)
            if hint_m:
                self.prompt_hints[hint_m.group(1).strip()] = hint_m.group(2).strip()

        # Reference images
        ref = sections.get("reference images", "")
        avatar_m = re.search(r'\*\*Avatar\*\*:\s*(.+)', ref)
        if avatar_m:
            self.avatar_filename = avatar_m.group(1).strip()
        ref_m = re.search(r'\*\*Reference\*\*:\s*(.+)', ref)
        if ref_m:
            self.reference_filename = ref_m.group(1).strip()


# Per-user config cache: {user_id: (CharacterConfig, soul_path)}
_configs: dict[str, tuple[CharacterConfig, str]] = {}


def update_soul_fields(text: str, fields: dict) -> str:
    """Edit only supplied fields, preserving other sections and identity lines."""
    if not isinstance(fields, dict):
        raise ValueError("character fields must be an object")
    parts = re.split(r"(?m)(?=^#[ \t]+)", text)

    def edit_section(title, edit):
        for i, part in enumerate(parts):
            heading = re.match(r"#[ \t]+([^\r\n]+)\r?\n?", part)
            if heading and heading.group(1).strip().lower() == title.lower():
                parts[i] = heading.group(0).rstrip() + "\n" + edit(part[heading.end():])
                return
        parts.append("\n# " + title + "\n" + edit(""))

    def edit_line(body, pattern, replacement):
        if re.search(pattern, body, re.MULTILINE):
            return re.sub(pattern, lambda _: replacement, body, count=1, flags=re.MULTILINE)
        return body.rstrip() + "\n" + replacement + "\n\n"

    for field, label in (("name", "Name"), ("user_address", "User Address")):
        if field in fields:
            value = fields[field]
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ValueError(f"{field} must be a single line of text")
            edit_section("Identity", lambda body, label=label, value=value: edit_line(
                body, r"^.*\*\*" + label + r"\*\*:[^\r\n]*", f"- **{label}**: {value}"))

    sections = {"personality": "Personality", "speech_patterns": "Speech Patterns",
                "appearance": "Appearance", "backstory": "Backstory", "interests": "Interests"}
    for field, title in sections.items():
        if field in fields:
            value = fields[field]
            if not isinstance(value, str):
                raise ValueError(f"{field} must be text")
            edit_section(title, lambda _, value=value: "\n" + value.rstrip() + "\n\n")
    if "prompt_hints" in fields:
        hints = fields["prompt_hints"]
        if not isinstance(hints, dict):
            raise ValueError("prompt_hints must be an object")
        for key, value in hints.items():
            if (not isinstance(key, str) or not re.fullmatch(r"\w+", key)
                    or not isinstance(value, str) or "\n" in value or "\r" in value):
                raise ValueError("prompt hints must have word keys and single-line text values")
            edit_section("Prompt Hints", lambda body, key=key, value=value: edit_line(
                body, r"^-[ \t]+" + re.escape(key) + r":[^\r\n]*", f"- {key}: {value}"))
    return "".join(parts)


def _current_user_id() -> str:
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def _resolve_soul_path() -> str:
    """Determine which soul.md to load: active model's persona or project default."""
    try:
        import model_library
        return model_library.get_active_soul_path()
    except Exception:
        return _SOUL_PATH


def get_config():
    """Return per-user CharacterConfig. Loads soul.md on first call.

    Automatically uses the active model's persona soul.md if available.
    Cached per-user to avoid cross-user config leaks.
    """
    uid = _current_user_id()
    soul_path = _resolve_soul_path()
    try:
        stat = os.stat(soul_path)
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None

    cached = _configs.get(uid)
    if cached is not None:
        cfg, cached_path = cached
        # Auto-invalidate if the active model changed
        if cached_path == soul_path and getattr(cfg, "_source_signature", None) == signature:
            return cfg

    cfg = CharacterConfig()
    cfg._source_signature = signature
    if os.path.exists(soul_path):
        with open(soul_path, "r", encoding="utf-8") as f:
            cfg._parse(f.read())
    _configs[uid] = (cfg, soul_path)
    return cfg


def reload_config():
    """Force reload soul.md for current user (e.g. after editing or model switch)."""
    uid = _current_user_id()
    _configs.pop(uid, None)
    return get_config()

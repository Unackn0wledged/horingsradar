"""Versioned prompts in prompts/assess_vN.md.

A prompt file holds the system prompt, a `<!-- user -->` separator and a Jinja2 template for the
user message. Existing versions are never edited; new versions get a new file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

SEPARATOR = "<!-- user -->"
_env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    user_template: str

    def render_user(self, **context: Any) -> str:
        return _env.from_string(self.user_template).render(**context)


def load_prompt(prompts_dir: Path, version: str) -> Prompt:
    path = prompts_dir / f"assess_{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt {version} not found: {path}")
    content = path.read_text(encoding="utf-8")
    if SEPARATOR not in content:
        raise ValueError(f"{path} is missing the '{SEPARATOR}' separator")
    system, user = content.split(SEPARATOR, 1)
    return Prompt(version=version, system=system.strip(), user_template=user.lstrip("\n"))

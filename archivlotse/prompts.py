from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

DEFAULT_PROMPTS_DIR = "config/prompts"


def _make_env(prompts_dir: str | Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def load_prompt_source(name: str, prompts_dir: str | Path = DEFAULT_PROMPTS_DIR) -> str:
    return (Path(prompts_dir) / name).read_text(encoding="utf-8")


def render_prompt(name: str, prompts_dir: str | Path = DEFAULT_PROMPTS_DIR, **context) -> str:
    template = _make_env(prompts_dir).get_template(name)
    return template.render(**context)

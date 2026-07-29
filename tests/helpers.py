import contextlib
import importlib
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = ROOT / "local_sdlc.py"
PRODUCT_NAME_PARTS = (
    ("clau", "de"),
    ("anth", "ropic"),
    ("co", "dex"),
)


def product_name_pattern() -> str:
    names = ("".join(parts) for parts in PRODUCT_NAME_PARTS)
    return r"(?i)(?:" + "|".join(names) + r")"


def load_module():
    module = importlib.import_module("local_sdlc.cli")
    return importlib.reload(module)


class LocalSDLCTestCase(unittest.TestCase):
    def setUp(self):
        self.local_sdlc = load_module()

    def make_agent_project(self, root: Path, spec: str = "# SPEC\n") -> tuple[Path, Path]:
        project = root / "project"
        project.mkdir()
        skills_dir = root / "skills"
        for name in ("sdlc", "tdd", "review"):
            skill_dir = skills_dir / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
                encoding="utf-8",
            )
        (project / "SPEC.md").write_text(spec, encoding="utf-8")
        return project, skills_dir

    @contextlib.contextmanager
    def scrub_llm_env(self, extra: dict[str, str] | None = None):
        keys = {
            "LOCAL_SDLC_API_KEY",
            "LOCAL_LLM_BASE_URL",
            "OPENAI_BASE_URL",
            "LOCAL_LLM_API_KEY",
            "OPENAI_API_KEY",
            "LOCAL_LLM_MODEL",
        }
        if extra:
            keys.update(extra)
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            if extra:
                os.environ.update(extra)
            yield
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

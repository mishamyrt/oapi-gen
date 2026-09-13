import tomllib
from pathlib import Path

from oapi_gen import __version__


def test_packages_share_the_generator_version() -> None:
    root = Path(__file__).resolve().parents[1]
    for project_file in [root / "pyproject.toml", *root.glob("integrations/*/pyproject.toml")]:
        project = tomllib.loads(project_file.read_text())["project"]
        assert project["version"] == __version__, project_file

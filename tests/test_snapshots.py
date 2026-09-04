from pathlib import Path

import pytest
from oapi_gen import generate_package


@pytest.mark.parametrize("name", ["cats", "advanced"])
def test_generated_package_matches_snapshot(tmp_path: Path, name: str) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    snapshots = Path(__file__).parent / "snapshots" / name
    output = tmp_path / "generated"

    generate_package(fixtures / f"{name}.openapi.yaml", output)

    expected = {path.name.removesuffix(".txt"): path.read_bytes() for path in snapshots.iterdir()}
    assert {path.name for path in output.iterdir()} == expected.keys()
    for filename, content in expected.items():
        assert (output / filename).read_bytes() == content, filename

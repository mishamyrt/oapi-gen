from pathlib import Path

import pytest
from oapi_gen import generate_package


@pytest.mark.parametrize("name", ["cats", "advanced", "streaming"])
def test_generated_package_matches_snapshot(tmp_path: Path, name: str) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    snapshots = Path(__file__).parent / "snapshots" / name
    output = tmp_path / "generated"

    generate_package(fixtures / f"{name}.openapi.yaml", output)

    expected = {
        str(path.relative_to(snapshots)).removesuffix(".txt"): path.read_bytes()
        for path in snapshots.rglob("*")
        if path.is_file()
    }
    assert {
        str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()
    } == expected.keys()
    for filename, content in expected.items():
        assert (output / filename).read_bytes() == content, filename

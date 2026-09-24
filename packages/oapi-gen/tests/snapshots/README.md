# Generated package snapshots

Each directory contains the complete output of its matching OpenAPI fixture,
including the manifest. The `.txt` suffix keeps generated Python examples outside
test collection and source linting.

`test_snapshots.py` compares both the file list and every file's bytes. These
snapshots cover the Starlette router, msgspec models, HTTP helpers and OpenAPI JSON.

When intentionally changing generated output, generate the matching fixture into
a temporary directory, inspect the diff, and copy each output file here with
`.txt` appended to its name, preserving the `contracts/` and `routes/` subdirectories.
Include `.oapi-gen-manifest.json`. Do not regenerate
snapshots to make a structural refactor pass.

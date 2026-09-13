VERSION = 0.1.8

.PHONY: lint
lint:
	@for project in packages/*; do uv run --all-packages --directory "$$project" ruff check . || exit $$?; done

.PHONY: test
test:
	@for project in packages/*; do uv run --all-packages --directory "$$project" pytest || exit $$?; done

.PHONY: typecheck
typecheck:
	@for project in packages/*; do uv run --all-packages --directory "$$project" basedpyright || exit $$?; done

.PHONY: fmt
fmt:
	@for project in packages/*; do uv run --all-packages --directory "$$project" ruff check --select I --fix || exit $$?; done
	@for project in packages/*; do uv run --all-packages --directory "$$project" ruff format . || exit $$?; done

.PHONY: publish
publish:
	@test -z "$$(git status --porcelain -- . ':!Makefile')" || { echo "Commit other changes before publishing; only Makefile may be modified."; exit 1; }
	@git check-ref-format "refs/tags/v$(VERSION)"
	@if git show-ref --verify --quiet "refs/tags/v$(VERSION)"; then echo "Tag v$(VERSION) already exists."; exit 1; fi
	@test "$$(uv version --dry-run --frozen --short "$(VERSION)")" = "$(VERSION)" || { echo "VERSION must be a canonical Python package version."; exit 1; }
	@for project in . packages/*; do uv version --project "$$project" --frozen "$(VERSION)" || exit $$?; done
	uv lock
	uv sync --locked --all-packages
	uv run --no-sync python scripts/update_snapshot_versions.py
	git add -- Makefile pyproject.toml packages/*/pyproject.toml uv.lock packages/oapi-gen/tests/snapshots
	git commit --allow-empty -m "chore: release v$(VERSION)"
	git tag -a "v$(VERSION)" -m "Release v$(VERSION)"

.PHONY: lint
lint:
	uv run ruff check .

.PHONY: typecheck
typecheck:
	uv run basedpyright "$(SRC)" "$(MIGRATIONS)"

.PHONY: fmt
fmt:
	uv run ruff check --select I --fix
	uv run ruff format .

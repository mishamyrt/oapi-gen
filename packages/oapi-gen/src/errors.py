class GenerationError(Exception):
    """Raised when an OpenAPI document cannot be generated safely."""


class CheckFailedError(GenerationError):
    """Raised when generated files are missing or stale."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__("generated files are stale: " + ", ".join(paths))

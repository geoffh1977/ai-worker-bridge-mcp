from __future__ import annotations


class WorkingDirectoryError(RuntimeError):
    """Base class for invalid or missing task working-directory declarations."""

    status_code = 400


class InvalidWorkingDirectory(WorkingDirectoryError):
    """Raised before dispatch when a task selects an unusable working directory."""

    status_code = 400

    def __init__(self, path: str, allowed_paths: list[str]):
        self.path = path
        self.allowed_paths = allowed_paths
        allowed = ", ".join(allowed_paths) if allowed_paths else "(none)"
        super().__init__(
            f"Specified working_directory '{path}' not in allowed paths: {allowed}"
        )


class MissingWorkingDirectory(WorkingDirectoryError):
    """Raised when a task does not declare a working directory in frontmatter."""

    def __init__(self):
        super().__init__("working_directory is required in YAML frontmatter")

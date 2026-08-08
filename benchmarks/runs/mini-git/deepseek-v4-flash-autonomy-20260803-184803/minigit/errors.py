"""Public error hierarchy for minigit."""


class MiniGitError(Exception):
    """Base class for all minigit domain errors."""


class CorruptObjectError(MiniGitError):
    """Raised when an object is missing, malformed, or digest-mismatched."""


class DirtyWorktreeError(MiniGitError):
    """Raised when checkout would overwrite dirty worktree state."""


class InvalidPathError(MiniGitError):
    """Raised when a path is unsafe or escapes the repository."""


class NothingToCommitError(MiniGitError):
    """Raised when a commit has no changes relative to HEAD."""


class RepositoryNotFoundError(MiniGitError):
    """Raised when opening a repository that is not initialized."""


class RevisionNotFoundError(MiniGitError):
    """Raised when a revision cannot be resolved."""

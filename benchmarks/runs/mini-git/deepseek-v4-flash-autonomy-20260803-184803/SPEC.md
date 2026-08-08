# Mini Git Implementation Specification

## Purpose

Build a small, persistent, Git-like version-control system named `minigit`.
It must be implemented with the Python standard library only and must not call
the system `git` command. The benchmark evaluates behavior, persistence,
failure handling, and autonomous completion from this specification.

## Fixed Constraints

- Target Python 3.10 or later.
- Use only the Python standard library.
- Do not invoke or import Git implementations or shell out to `git`.
- Store all repository state below `<repository>/.minigit/`.
- Treat `SPEC.md` and `acceptance_tests/` as read-only external evidence.
- Product code belongs in `minigit/`; agent-authored unit tests belong in `tests/`.
- Reject absolute paths, paths escaping the repository, symlinks, and paths inside `.minigit`.
- Persist every operation needed by a later Python process; process-local state is insufficient.

## Public Python API

The following imports and call signatures are required:

```python
from minigit.errors import (
    CorruptObjectError,
    DirtyWorktreeError,
    InvalidPathError,
    MiniGitError,
    NothingToCommitError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from minigit.objects import ObjectStore, hash_object
from minigit.repository import Repository
```

`ObjectStore`:

- `ObjectStore(git_dir: pathlib.Path)`
- `write(kind: str, payload: bytes) -> str`
- `read(oid: str) -> tuple[str, bytes]`

`Repository`:

- `Repository.init(path) -> Repository`
- `Repository.open(path) -> Repository`
- `add(paths) -> None`
- `commit(message, author="unknown", timestamp=None) -> str`
- `status() -> dict[str, list[str]]`
- `log(max_count=None) -> list[dict[str, object]]`
- `checkout(revision, force=False) -> str`
- read-only property `head_commit -> str | None`
- `resolve_revision(revision) -> str`

All public failures must derive from `MiniGitError`. `Repository.open()` outside
an initialized repository raises `RepositoryNotFoundError`.

## Repository Layout

After `Repository.init(root)`:

```text
root/
  .minigit/
    HEAD
    index.json
    objects/
    refs/
      heads/
        main
```

- `HEAD` initially contains `ref: refs/heads/main` followed by a newline.
- The `main` ref initially exists and is empty.
- The index is valid UTF-8 JSON and initially contains no paths.
- Calling `init` again on the same valid repository is idempotent and must not destroy data.

## Content-addressed Objects

For an ASCII object kind and byte payload, define:

```text
encoded = kind.encode("ascii") + b"\0" + payload
oid = sha256(encoded).hexdigest()
```

- Supported kinds are `blob`, `tree`, and `commit`.
- The object file is stored at `.minigit/objects/<oid[:2]>/<oid[2:]>` and contains `encoded` exactly.
- Writing identical input is idempotent and returns the same 64-character lowercase OID.
- `read` recomputes the digest. Missing, malformed, or digest-mismatched objects raise `CorruptObjectError`.
- Tree and commit JSON must use UTF-8, sorted keys, and compact separators so equivalent values are deterministic.

## Index And Add

- The index maps normalized POSIX repository-relative paths to blob OIDs.
- `add(["."])` recursively stages ordinary files while excluding `.minigit`.
- Explicit files and directories may be added.
- A tracked path that no longer exists is removed from the index when explicitly added, thereby staging deletion.
- Binary file bytes must round-trip without text decoding.
- An invalid path must fail before the index is changed.
- Index updates use a temporary file plus atomic replacement.

## Commits And Revisions

- A tree object contains the complete sorted mapping of indexed path to blob OID.
- A commit object contains `tree`, `parent`, `message`, `author`, and `timestamp`.
- `parent` is `null` for the first commit and the prior head OID thereafter.
- If `timestamp` is supplied, preserve its string representation exactly. Otherwise use a UTC ISO-8601 timestamp.
- Empty or whitespace-only messages are rejected with `MiniGitError`.
- A commit that has the same tree as HEAD raises `NothingToCommitError` and does not move HEAD.
- A successful commit updates the current branch ref atomically and returns its OID.
- `resolve_revision` accepts `HEAD`, a branch name, a full OID, or an unambiguous OID prefix. Unknown or ambiguous revisions raise `RevisionNotFoundError`.
- `log()` follows first parents from HEAD, newest first. Each item exposes at least `oid`, `tree`, `parent`, `message`, `author`, and `timestamp`.

## Status

`status()` returns exactly these keys, each containing sorted relative paths:

```python
{"staged": [], "modified": [], "deleted": [], "untracked": []}
```

- `staged`: the index differs from the HEAD tree for that path.
- `modified`: a present indexed working file differs from the indexed blob.
- `deleted`: an indexed working file is absent.
- `untracked`: an ordinary working file is absent from the index.
- `.minigit` content is never reported.

## Checkout

- `checkout(revision)` resolves and verifies the target commit and tree.
- If staged, modified, or deleted paths exist, raise `DirtyWorktreeError` before changing files unless `force=True`.
- Write every target tracked file with its exact blob bytes and create parent directories as needed.
- Remove paths tracked by the current commit but absent from the target commit.
- Preserve untracked files.
- Checking out an OID detaches HEAD by storing the full OID in `HEAD`; checking out a branch stores its symbolic ref.
- Return the resolved commit OID. On success, `head_commit` equals that OID and status is clean except for preserved untracked files.
- Validate all referenced object types and digests before mutating the working tree.

## Command-line Interface

The package must run as `python3 -m minigit`. The global repository option comes
before the subcommand:

```text
python3 -m minigit --repo PATH init
python3 -m minigit --repo PATH add PATH [PATH ...]
python3 -m minigit --repo PATH commit -m MESSAGE [--author NAME] [--timestamp VALUE]
python3 -m minigit --repo PATH status [--porcelain]
python3 -m minigit --repo PATH log [--oneline] [--max-count N]
python3 -m minigit --repo PATH checkout REVISION [--force]
```

- `commit` prints only the full commit OID followed by a newline.
- Porcelain status prints sorted lines using `A  path`, ` M path`, ` D path`, and `?? path`.
- Clean porcelain status prints no path lines.
- Oneline log prints `<12-character-oid-prefix> <message>`, newest first.
- Domain errors print a concise message to stderr and exit nonzero without a traceback.
- A successful command exits zero.

## Acceptance Criteria

- Repository initialization creates the required persistent layout and is idempotent.
- Object identity follows the specified SHA-256 formula and detects corrupted content.
- Add stages text and binary data, excludes metadata, and rejects unsafe paths without partial index changes.
- Two commits persist across reopen and form the correct parent chain.
- A no-change commit fails without moving HEAD.
- Status distinguishes staged, modified, deleted, and untracked paths.
- Checkout restores an earlier tree, preserves untracked files, and refuses dirty overwrite unless forced.
- Revision lookup handles HEAD, branches, full OIDs, and unambiguous prefixes.
- The CLI completes init, add, commit, status, log, and checkout in separate processes.
- All agent-authored unit tests and the fixed external acceptance suite pass.

## Implementation Stages

```json
{
  "stage_plan_schema": 1,
  "stages": [
    {
      "stage_id": "S01",
      "title": "Errors and content-addressed objects",
      "goal": "Create the package, public error hierarchy, deterministic object identity, persistent object storage, and corruption detection.",
      "writable_paths": ["minigit/__init__.py", "minigit/errors.py", "minigit/objects.py", "tests/test_objects.py"],
      "readonly_evidence_paths": ["SPEC.md", "acceptance_tests/test_minigit_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_objects.py' -v"],
      "required_observables": ["object identity and corruption tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off"],
      "max_rounds": 5
    },
    {
      "stage_id": "S02",
      "title": "Repository initialization and index",
      "goal": "Implement repository discovery, idempotent layout creation, safe path normalization, atomic index persistence, recursive add, deletion staging, and binary blobs.",
      "writable_paths": ["minigit/index.py", "minigit/repository.py", "tests/test_index.py", "tests/test_repository.py"],
      "readonly_evidence_paths": ["SPEC.md", "acceptance_tests/test_minigit_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_index.py' -v", "python3 -m unittest discover -s tests -p 'test_repository.py' -v"],
      "required_observables": ["repository and index tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "failure_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 6
    },
    {
      "stage_id": "S03",
      "title": "Commit graph and references",
      "goal": "Implement canonical tree and commit records, atomic symbolic and detached HEAD handling, revision resolution, commit creation, and first-parent log traversal.",
      "writable_paths": ["minigit/commits.py", "minigit/refs.py", "minigit/repository.py", "tests/test_commits.py"],
      "readonly_evidence_paths": ["SPEC.md", "acceptance_tests/test_minigit_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_commits.py' -v"],
      "required_observables": ["commit graph, revision, and persistence tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "failure_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 7
    },
    {
      "stage_id": "S04",
      "title": "Status and safe checkout",
      "goal": "Implement status classification and prevalidated checkout with dirty-worktree protection, tracked-file removal, exact byte restoration, and untracked-file preservation.",
      "writable_paths": ["minigit/status.py", "minigit/worktree.py", "minigit/repository.py", "tests/test_worktree.py"],
      "readonly_evidence_paths": ["SPEC.md", "acceptance_tests/test_minigit_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_worktree.py' -v"],
      "required_observables": ["status and checkout tests pass"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "failure_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 7
    },
    {
      "stage_id": "S05",
      "title": "CLI and integration documentation",
      "goal": "Implement the exact command-line contract, domain-error exits, process-level persistence, and concise usage documentation.",
      "writable_paths": ["minigit/cli.py", "minigit/__main__.py", "tests/test_cli.py", "README.md"],
      "readonly_evidence_paths": ["SPEC.md", "acceptance_tests/test_minigit_acceptance.py"],
      "test_commands": ["python3 -m unittest discover -s tests -p 'test_cli.py' -v"],
      "required_observables": ["CLI tests pass in separate processes"],
      "api_profile": ["generate_artifact:max_tokens=8192,temperature=0.05,thinking=off", "failure_analysis:max_tokens=8192,temperature=1,thinking=on"],
      "max_rounds": 7
    }
  ]
}
```

## Verification Commands

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s acceptance_tests -v
```

# minigit

A small, persistent, Git-like version-control system implemented with the
Python standard library only. It never invokes or imports Git.

## Usage

```bash
python3 -m minigit --repo PATH init
python3 -m minigit --repo PATH add PATH [PATH ...]
python3 -m minigit --repo PATH commit -m MESSAGE [--author NAME] [--timestamp VALUE]
python3 -m minigit --repo PATH status [--porcelain]
python3 -m minigit --repo PATH log [--oneline] [--max-count N]
python3 -m minigit --repo PATH checkout REVISION [--force]
```

## Public Python API

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

## Repository Layout

All state is stored below `<repository>/.minigit/`:

```text
.minigit/
  HEAD
  index.json
  objects/
  refs/
    heads/
      main
```

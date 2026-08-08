import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class MiniGitAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
        return target

    def run_cli(self, *args, expect=0):
        command = [
            sys.executable,
            "-m",
            "minigit",
            "--repo",
            str(self.root),
            *args,
        ]
        result = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(
            result.returncode,
            expect,
            msg=f"command={command!r}\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        return result

    def test_object_formula_persistence_and_corruption_detection(self):
        from minigit.errors import CorruptObjectError
        from minigit.objects import ObjectStore, hash_object

        git_dir = self.root / ".minigit"
        (git_dir / "objects").mkdir(parents=True)
        payload = b"hello\x00binary\xff"
        expected = hashlib.sha256(b"blob\x00" + payload).hexdigest()
        self.assertEqual(hash_object("blob", payload), expected)

        store = ObjectStore(git_dir)
        oid = store.write("blob", payload)
        self.assertEqual(oid, expected)
        self.assertEqual(store.write("blob", payload), expected)
        self.assertEqual(store.read(oid), ("blob", payload))

        object_path = git_dir / "objects" / oid[:2] / oid[2:]
        self.assertEqual(object_path.read_bytes(), b"blob\x00" + payload)
        object_path.write_bytes(b"blob\x00tampered")
        with self.assertRaises(CorruptObjectError):
            store.read(oid)

    def test_repository_layout_add_commit_and_reopen(self):
        from minigit.repository import Repository

        repo = Repository.init(self.root)
        self.assertEqual((self.root / ".minigit" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertTrue((self.root / ".minigit" / "refs" / "heads" / "main").exists())
        self.assertEqual(json.loads((self.root / ".minigit" / "index.json").read_text()), {})

        self.write("docs/readme.txt", "first\n")
        self.write("assets/raw.bin", b"\x00\xff\x10")
        repo.add(["."])
        first = repo.commit("initial", author="Ada", timestamp="2026-01-01T00:00:00Z")
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(
            repo.status(),
            {"staged": [], "modified": [], "deleted": [], "untracked": []},
        )

        reopened = Repository.open(self.root)
        self.assertEqual(reopened.head_commit, first)
        entry = reopened.log()[0]
        self.assertEqual(entry["oid"], first)
        self.assertEqual(entry["parent"], None)
        self.assertEqual(entry["message"], "initial")
        self.assertEqual(entry["author"], "Ada")
        self.assertEqual(entry["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual((self.root / "assets/raw.bin").read_bytes(), b"\x00\xff\x10")

        same = Repository.init(self.root)
        self.assertEqual(same.head_commit, first)

    def test_parent_chain_no_change_commit_and_revision_resolution(self):
        from minigit.errors import NothingToCommitError, RevisionNotFoundError
        from minigit.repository import Repository

        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")

        history = repo.log()
        self.assertEqual([item["oid"] for item in history], [second, first])
        self.assertEqual(history[0]["parent"], first)
        self.assertEqual(repo.resolve_revision("HEAD"), second)
        self.assertEqual(repo.resolve_revision("main"), second)
        self.assertEqual(repo.resolve_revision(first), first)
        self.assertEqual(repo.resolve_revision(first[:16]), first)
        with self.assertRaises(RevisionNotFoundError):
            repo.resolve_revision("deadbeef")

        before = repo.head_commit
        with self.assertRaises(NothingToCommitError):
            repo.commit("no changes", timestamp="3")
        self.assertEqual(repo.head_commit, before)

    def test_status_categories_and_add_is_atomic_on_invalid_path(self):
        from minigit.errors import InvalidPathError
        from minigit.repository import Repository

        repo = Repository.init(self.root)
        self.write("tracked.txt", "base")
        self.write("remove.txt", "remove")
        repo.add(["tracked.txt", "remove.txt"])
        repo.commit("base", timestamp="1")

        self.write("tracked.txt", "working")
        (self.root / "remove.txt").unlink()
        self.write("new.txt", "new")
        self.assertEqual(
            repo.status(),
            {
                "staged": [],
                "modified": ["tracked.txt"],
                "deleted": ["remove.txt"],
                "untracked": ["new.txt"],
            },
        )

        index_before = (self.root / ".minigit" / "index.json").read_bytes()
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(InvalidPathError):
            repo.add(["tracked.txt", "../outside.txt"])
        self.assertEqual((self.root / ".minigit" / "index.json").read_bytes(), index_before)

        repo.add(["tracked.txt", "remove.txt"])
        after = repo.status()
        self.assertEqual(after["modified"], [])
        self.assertEqual(after["deleted"], [])
        self.assertEqual(after["staged"], ["remove.txt", "tracked.txt"])
        with self.assertRaises(InvalidPathError):
            repo.add([str(outside.resolve())])
        with self.assertRaises(InvalidPathError):
            repo.add([".minigit/HEAD"])

    def test_checkout_restores_tree_preserves_untracked_and_blocks_dirty(self):
        from minigit.errors import DirtyWorktreeError
        from minigit.repository import Repository

        repo = Repository.init(self.root)
        self.write("dir/data.txt", "version one")
        self.write("old.txt", "old")
        repo.add(["."])
        first = repo.commit("first", timestamp="1")

        self.write("dir/data.txt", "version two")
        (self.root / "old.txt").unlink()
        self.write("new-tracked.txt", "new tracked")
        repo.add(["dir/data.txt", "old.txt", "new-tracked.txt"])
        second = repo.commit("second", timestamp="2")
        self.assertNotEqual(first, second)

        self.write("keep.local", "untracked")
        resolved = repo.checkout(first)
        self.assertEqual(resolved, first)
        self.assertEqual(repo.head_commit, first)
        self.assertEqual((self.root / "dir/data.txt").read_text(), "version one")
        self.assertEqual((self.root / "old.txt").read_text(), "old")
        self.assertFalse((self.root / "new-tracked.txt").exists())
        self.assertEqual((self.root / "keep.local").read_text(), "untracked")
        self.assertEqual(repo.status()["untracked"], ["keep.local"])

        self.write("dir/data.txt", "dirty")
        with self.assertRaises(DirtyWorktreeError):
            repo.checkout(second)
        self.assertEqual((self.root / "dir/data.txt").read_text(), "dirty")
        repo.checkout(second, force=True)
        self.assertEqual((self.root / "dir/data.txt").read_text(), "version two")

    def test_cli_end_to_end_across_processes(self):
        self.run_cli("init")
        self.write("note.txt", "v1")
        self.run_cli("add", "note.txt")
        first_result = self.run_cli(
            "commit",
            "-m",
            "first commit",
            "--author",
            "CLI User",
            "--timestamp",
            "1",
        )
        first = first_result.stdout.strip()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(self.run_cli("status", "--porcelain").stdout, "")

        self.write("note.txt", "v2")
        status = self.run_cli("status", "--porcelain").stdout.splitlines()
        self.assertEqual(status, [" M note.txt"])
        self.run_cli("add", "note.txt")
        second = self.run_cli("commit", "-m", "second commit", "--timestamp", "2").stdout.strip()
        lines = self.run_cli("log", "--oneline", "--max-count", "2").stdout.splitlines()
        self.assertEqual(lines, [f"{second[:12]} second commit", f"{first[:12]} first commit"])

        self.run_cli("checkout", first)
        self.assertEqual((self.root / "note.txt").read_text(), "v1")
        self.assertEqual(self.run_cli("status", "--porcelain").stdout, "")

    def test_cli_domain_error_is_concise(self):
        result = self.run_cli("status", "--porcelain", expect=1)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()

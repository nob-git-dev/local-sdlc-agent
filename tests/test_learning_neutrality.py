import unittest
from pathlib import Path


class LearningNeutralityTests(unittest.TestCase):
    def test_learning_control_plane_contains_no_benchmark_named_branch(self):
        root = Path(__file__).resolve().parents[1]
        paths = sorted((root / "learning_runtime").glob("*.py"))
        paths.extend(sorted((root / "local_sdlc").glob("learning_*.py")))
        benchmark_names = ("tet" + "ris", "mini_" + "sqlite", "red" + "is")

        violations = []
        for path in paths:
            lowered = path.read_text(encoding="utf-8").lower()
            if any(name in lowered for name in benchmark_names):
                violations.append(str(path.relative_to(root)))

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()

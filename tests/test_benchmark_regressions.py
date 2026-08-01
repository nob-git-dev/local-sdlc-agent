import unittest

from benchmarks.run_regressions import _tetris_false_positive_result, _unknown_scope_result


class BenchmarkRegressionTests(unittest.TestCase):
    def test_known_tetris_false_positive_is_rejected(self):
        result = _tetris_false_positive_result(30.0)

        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["observed_active_piece_motion_failure"])

    def test_unknown_domain_does_not_receive_tetris_memory(self):
        result = _unknown_scope_result()

        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["domain_rule_leaked"])


if __name__ == "__main__":
    unittest.main()

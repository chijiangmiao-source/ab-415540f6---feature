"""Rule-logic (JTMS) test suite — runs with the plain stdlib unittest."""

import os
import tempfile
import unittest

from app.tms import Conflict, Engine, NotFound, ValidationFailed


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "tms.db")
        self.engine = Engine(self.db)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- helpers

    def conclusion(self, node):
        for item in self.engine.state()["conclusions"]:
            if item["id"] == node:
                return item
        raise AssertionError(f"no conclusion {node!r}")

    def assert_valid(self, node):
        self.assertTrue(self.conclusion(node)["valid"], f"{node} invalid")

    def assert_invalid(self, node):
        self.assertFalse(self.conclusion(node)["valid"], f"{node} valid")

    def build_two_path_procedure(self):
        """Safety procedure: C supported by two independent paths, D below C."""
        self.engine.add_fact("F1", "sensor A reading")
        self.engine.add_fact("F2", "sensor B reading")
        self.engine.add_rules({"id": "R1", "premises": ["F1"],
                               "conclusion": "C"})
        self.engine.add_rules({"id": "R2", "premises": ["F2"],
                               "conclusion": "C"})
        self.engine.add_rules({"id": "R3", "premises": ["C"],
                               "conclusion": "D"})

    # ------------------------------------------------------- core scenarios

    def test_two_independent_supports_keep_conclusion_alive(self):
        self.build_two_path_procedure()
        self.assert_valid("C")
        self.assert_valid("D")

        verdict = self.engine.retract_fact("F1")
        # Conclusion stays valid on the remaining independent support.
        self.assert_valid("C")
        self.assert_valid("D")
        self.assertEqual(verdict["invalidated"], [])
        retained = {r["node"]: r for r in verdict["retained"]}
        self.assertIn("C", retained)
        remaining = retained["C"]["remaining_supports"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["rule_id"], "R2")
        self.assertEqual(remaining[0]["premises"], ["F2"])

    def test_last_support_retraction_cascades_downstream(self):
        self.build_two_path_procedure()
        self.engine.retract_fact("F1")
        verdict = self.engine.retract_fact("F2")

        self.assert_invalid("C")
        self.assert_invalid("D")
        invalidated = [item["node"] for item in verdict["invalidated"]]
        self.assertEqual(invalidated, ["C", "D"])
        # Propagation chain: C exhausts its support first, then D.
        chain = [(step["depth"], step["node"], step["cause"])
                 for step in verdict["propagation"]]
        self.assertEqual(chain, [
            (0, "C", "support-exhausted"),
            (1, "D", "support-exhausted"),
        ])

    def test_complete_premise_set_saved_per_firing(self):
        self.engine.add_fact("F1")
        self.engine.add_fact("F2")
        self.engine.add_rules({"id": "R1", "premises": ["F1", "F2"],
                               "conclusion": "C"})
        supports = self.conclusion("C")["supports"]
        self.assertEqual(len(supports), 1)
        self.assertEqual(supports[0]["rule_id"], "R1")
        self.assertEqual(supports[0]["premises"], ["F1", "F2"])
        self.assertEqual(supports[0]["status"], "valid")

    def test_repeated_retraction_replays_same_verdict(self):
        self.build_two_path_procedure()
        first = self.engine.retract_fact("F1")
        second = self.engine.retract_fact("F1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        for key in ("fact_id", "verdict", "at", "invalidated", "retained",
                    "propagation"):
            self.assertEqual(first[key], second[key], key)

    def test_unknown_fact_retraction_rejected(self):
        with self.assertRaises(NotFound):
            self.engine.retract_fact("NOPE")

    def test_rule_with_unknown_premise_rejected_without_pollution(self):
        self.engine.add_fact("F1")
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules({"id": "R1", "premises": ["GHOST"],
                                   "conclusion": "C"})
        state = self.engine.state()
        self.assertEqual(state["rules"], [])
        self.assertEqual(state["conclusions"], [])

    def test_self_supporting_loop_rejected(self):
        self.engine.add_fact("F1")
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules({"id": "R1", "premises": ["F1", "C"],
                                   "conclusion": "C"})
        self.assertEqual(self.engine.state()["rules"], [])

    def test_batch_is_atomic(self):
        self.engine.add_fact("F1")
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules([
                {"id": "R1", "premises": ["F1"], "conclusion": "C"},
                {"id": "R2", "premises": ["GHOST"], "conclusion": "E"},
            ])
        state = self.engine.state()
        self.assertEqual(state["rules"], [])
        self.assertEqual(state["conclusions"], [])

    def test_cycle_alone_derives_nothing(self):
        self.engine.add_fact("F1")
        self.engine.add_rules([
            {"id": "R1", "premises": ["X"], "conclusion": "Y"},
            {"id": "R2", "premises": ["Y"], "conclusion": "X"},
        ])
        self.assert_invalid("X")
        self.assert_invalid("Y")

    def test_grounded_cycle_collapses_when_ground_retracted(self):
        self.engine.add_fact("F1")
        self.engine.add_rules({"id": "R0", "premises": ["F1"],
                               "conclusion": "X"})
        self.engine.add_rules([
            {"id": "R1", "premises": ["X"], "conclusion": "Y"},
            {"id": "R2", "premises": ["Y"], "conclusion": "X"},
        ])
        self.assert_valid("X")
        self.assert_valid("Y")

        self.engine.retract_fact("F1")
        # The X<->Y loop must not keep itself alive without ground support.
        self.assert_invalid("X")
        self.assert_invalid("Y")

    def test_reassert_restores_conclusions(self):
        self.build_two_path_procedure()
        self.engine.retract_fact("F1")
        self.engine.retract_fact("F2")
        self.assert_invalid("C")
        verdict = self.engine.assert_fact("F2")
        self.assert_valid("C")
        self.assert_valid("D")
        self.assertEqual(verdict["restored"], ["C", "D"])

    def test_justification_tree_is_complete(self):
        self.build_two_path_procedure()
        self.engine.retract_fact("F1")
        tree = self.engine.justification("D")
        self.assertTrue(tree["valid"])
        self.assertEqual(tree["supports"][0]["rule_id"], "R3")
        premise_c = tree["supports"][0]["premises"][0]
        self.assertEqual(premise_c["node"], "C")
        valid_supports = [s for s in premise_c["supports"]
                          if s["status"] == "valid"]
        self.assertEqual(len(valid_supports), 1)
        self.assertEqual(valid_supports[0]["rule_id"], "R2")
        leaf = valid_supports[0]["premises"][0]
        self.assertEqual(leaf, {"node": "F2", "kind": "fact",
                                "label": "sensor B reading",
                                "status": "asserted", "valid": True})

    def test_justification_of_unknown_node_rejected(self):
        with self.assertRaises(NotFound):
            self.engine.justification("NOPE")

    def test_duplicate_ids_rejected(self):
        self.engine.add_fact("F1")
        with self.assertRaises(Conflict):
            self.engine.add_fact("F1")
        self.engine.add_rules({"id": "R1", "premises": ["F1"],
                               "conclusion": "C"})
        with self.assertRaises(Conflict):
            self.engine.add_rules({"id": "R1", "premises": ["F1"],
                                   "conclusion": "C2"})
        with self.assertRaises(Conflict):
            self.engine.add_fact("C")  # collides with a conclusion

    def test_persistence_across_restart(self):
        self.build_two_path_procedure()
        verdict = self.engine.retract_fact("F1")
        self.engine.close()

        reopened = Engine(self.db)
        try:
            state = reopened.state()
            facts = {f["id"]: f for f in state["facts"]}
            self.assertEqual(facts["F1"]["status"], "retracted")
            self.assertEqual(facts["F2"]["status"], "asserted")
            conclusions = {c["id"]: c for c in state["conclusions"]}
            self.assertTrue(conclusions["C"]["valid"])
            self.assertTrue(conclusions["D"]["valid"])
            # Justification state survives the restart as well.
            tree = reopened.justification("C")
            valid_supports = [s for s in tree["supports"]
                              if s["status"] == "valid"]
            self.assertEqual([s["rule_id"] for s in valid_supports], ["R2"])
            # And the recorded verdict is replayed identically.
            replay = reopened.retract_fact("F1")
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["at"], verdict["at"])
        finally:
            reopened.close()

    def test_health_reflects_store(self):
        self.assertEqual(self.engine.health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

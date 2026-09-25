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

    # ---------------------------------------------------- negative premises

    def build_release_gate_procedure(self):
        """C keeps two positive supports; E is released only while the
        blocking fact B is absent; G sits downstream of E."""
        self.build_two_path_procedure()
        self.engine.add_fact("B", "blocking fact")
        self.engine.add_rules(
            {"id": "RN", "premises": ["F1", {"id": "B", "polarity": "neg"}],
             "conclusion": "C"})
        self.engine.add_rules(
            {"id": "RE", "premises": ["C", {"id": "B", "polarity": "neg"}],
             "conclusion": "E"})
        self.engine.add_rules({"id": "RG", "premises": ["E"],
                               "conclusion": "G"})

    def test_negative_premise_blocked_while_blocker_asserted(self):
        self.build_release_gate_procedure()
        self.assert_valid("C")
        self.assert_valid("D")
        self.assert_invalid("E")
        self.assert_invalid("G")
        rules = {r["id"]: r for r in self.engine.state()["rules"]}
        self.assertFalse(rules["RE"]["firing"])
        self.assertEqual(rules["RE"]["blocked_by"], ["B"])
        # The negated premise is explicit in the wire form of the rule.
        self.assertEqual(rules["RE"]["premises"],
                         ["C", {"id": "B", "polarity": "neg"}])

    def test_blocker_assertion_invalidates_and_withdrawal_restores(self):
        self.build_release_gate_procedure()
        # Withdraw the blocker: the negative support fires and coexists
        # with the two original positive supports of C.
        verdict = self.engine.retract_fact("B")
        self.assertEqual(verdict["invalidated"], [])
        self.assert_valid("E")
        self.assert_valid("G")
        live = sorted(s["rule_id"] for s in self.conclusion("C")["supports"]
                      if s["status"] == "valid")
        self.assertEqual(live, ["R1", "R2", "RN"])

        # Asserting the blocker: everything relying on its absence falls,
        # including downstream G; C and D survive on positive supports.
        verdict = self.engine.assert_fact("B")
        self.assertFalse(verdict["replayed"])
        self.assertEqual([i["node"] for i in verdict["invalidated"]],
                         ["E", "G"])
        self.assert_invalid("E")
        self.assert_invalid("G")
        self.assert_valid("C")
        self.assert_valid("D")
        chain = [(s["depth"], s["node"], s["cause"])
                 for s in verdict["propagation"]]
        self.assertEqual(chain, [(0, "E", "negative-premise-blocked"),
                                 (1, "G", "support-exhausted")])
        self.assertEqual(verdict["propagation"][0]["blocked_by"], ["B"])
        lost = verdict["invalidated"][0]["lost_supports"]
        self.assertEqual(lost[0]["rule_id"], "RE")
        self.assertEqual(lost[0]["broken_premises"],
                         [{"id": "B", "polarity": "neg"}])

        # Withdrawing it again restores the dependent conclusions, and the
        # negative support coexists with the two positive ones once more.
        self.engine.retract_fact("B")
        self.assert_valid("E")
        self.assert_valid("G")
        live = sorted(s["rule_id"] for s in self.conclusion("C")["supports"]
                      if s["status"] == "valid")
        self.assertEqual(live, ["R1", "R2", "RN"])

    def test_retracting_blocker_reports_restored(self):
        self.build_release_gate_procedure()
        verdict = self.engine.retract_fact("B")
        self.assertEqual(verdict["restored"], ["E", "G"])
        # Replay of the same retraction is byte-identical.
        replay = self.engine.retract_fact("B")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["restored"], ["E", "G"])

    def test_negative_premise_on_conclusion(self):
        self.engine.add_fact("F1")
        self.engine.add_rules({"id": "R1", "premises": ["F1"],
                               "conclusion": "C"})
        self.engine.add_rules(
            {"id": "R2", "premises": [{"id": "C", "polarity": "neg"}],
             "conclusion": "E"})
        self.assert_invalid("E")  # C holds, so the exception is blocked
        self.engine.retract_fact("F1")
        self.assert_valid("E")    # C fell: absence confirmed, gate opens

    def test_justification_marks_negative_premises(self):
        self.build_release_gate_procedure()
        tree = self.engine.justification("E")
        self.assertFalse(tree["valid"])
        support = tree["supports"][0]
        self.assertEqual(support["status"], "invalid")
        pos_leaf, neg_leaf = support["premises"]
        self.assertNotIn("polarity", pos_leaf)
        self.assertEqual(neg_leaf["node"], "B")
        self.assertEqual(neg_leaf["polarity"], "neg")
        self.assertFalse(neg_leaf["premise_met"])  # B present: blocked

        self.engine.retract_fact("B")
        tree = self.engine.justification("E")
        self.assertTrue(tree["valid"])
        neg_leaf = tree["supports"][0]["premises"][1]
        self.assertTrue(neg_leaf["premise_met"])   # absence confirmed

    def test_negative_cycle_rejected_without_pollution(self):
        self.engine.add_fact("F1")
        self.engine.add_rules({"id": "R0", "premises": ["F1"],
                               "conclusion": "Y"})
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules([
                {"id": "RN1", "premises": [{"id": "Y", "polarity": "neg"}],
                 "conclusion": "X"},
                {"id": "RN2", "premises": ["X"], "conclusion": "Y"},
            ])
        state = self.engine.state()
        self.assertEqual({r["id"] for r in state["rules"]}, {"R0"})
        self.assertNotIn("X", {c["id"] for c in state["conclusions"]})
        self.assert_valid("Y")

    def test_rule_closing_cycle_through_existing_negative_edge_rejected(self):
        self.engine.add_fact("F1")
        self.engine.add_rules({"id": "R1", "premises": ["F1"],
                               "conclusion": "Y"})
        self.engine.add_rules(
            {"id": "R2", "premises": [{"id": "Y", "polarity": "neg"}],
             "conclusion": "X"})
        # X -> Y would close a cycle through the negative edge Y -/-> X.
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules({"id": "R3", "premises": ["X"],
                                   "conclusion": "Y"})
        self.assertNotIn("R3", {r["id"] for r in self.engine.state()["rules"]})

    def test_negative_self_loop_rejected(self):
        self.engine.add_fact("F1")
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules(
                {"id": "RNEG", "premises": [{"id": "C", "polarity": "neg"}],
                 "conclusion": "C"})
        self.assertEqual(self.engine.state()["rules"], [])

    def test_unknown_negative_premise_rejected(self):
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules(
                {"id": "RBAD", "premises": [{"id": "GHOST",
                                             "polarity": "neg"}],
                 "conclusion": "E"})
        self.assertEqual(self.engine.state()["rules"], [])

    def test_conflicting_polarity_rejected(self):
        self.engine.add_fact("B")
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules(
                {"id": "RBAD",
                 "premises": ["B", {"id": "B", "polarity": "neg"}],
                 "conclusion": "E"})
        self.assertEqual(self.engine.state()["rules"], [])

    def test_negative_supports_survive_restart(self):
        self.build_release_gate_procedure()
        self.engine.retract_fact("B")
        self.engine.close()

        reopened = Engine(self.db)
        try:
            conclusions = {c["id"]: c
                           for c in reopened.state()["conclusions"]}
            self.assertTrue(conclusions["E"]["valid"])
            rn = [s for s in conclusions["C"]["supports"]
                  if s["rule_id"] == "RN"][0]
            self.assertEqual(rn["status"], "valid")
            self.assertEqual(rn["premises"],
                             ["F1", {"id": "B", "polarity": "neg"}])
            tree = reopened.justification("E")
            neg_leaf = tree["supports"][0]["premises"][1]
            self.assertEqual(neg_leaf["polarity"], "neg")
            self.assertTrue(neg_leaf["premise_met"])
        finally:
            reopened.close()

    def test_all_positive_wire_format_unchanged(self):
        self.build_two_path_procedure()
        state = self.engine.state()
        rule = {r["id"]: r for r in state["rules"]}["R1"]
        self.assertEqual(rule["premises"], ["F1"])
        self.assertNotIn("blocked_by", rule)
        support = self.conclusion("C")["supports"][0]
        self.assertEqual(support["premises"], ["F1"])
        verdict = self.engine.retract_fact("F1")
        self.assertNotIn("restored", verdict)


if __name__ == "__main__":
    unittest.main()

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

    def build_exception_procedure(self):
        """Exception procedure: C is released only when F1 holds AND the
        blocking fact B is absent; D derives from C.  Two extra independent
        positive paths (R2 via F2) also support C so it survives while B is
        asserted."""
        self.engine.add_fact("F1", "primary condition")
        self.engine.add_fact("F2", "backup condition")
        self.engine.add_fact("B", "blocking hazard")
        self.engine.retract_fact("B")  # the blocker is initially absent
        self.engine.add_rules({
            "id": "RN",
            "premises": ["F1", {"node": "B", "polarity": "negative"}],
            "conclusion": "C",
        })
        self.engine.add_rules({"id": "R2", "premises": ["F2"],
                               "conclusion": "C"})
        self.engine.add_rules({"id": "R3", "premises": ["C"],
                               "conclusion": "D"})

    # -------------------------------------------------- negation scenarios

    def test_negative_premise_satisfied_by_absence(self):
        self.build_exception_procedure()
        self.assert_valid("C")
        self.assert_valid("D")
        support = next(s for s in self.conclusion("C")["supports"]
                       if s["rule_id"] == "RN")
        self.assertEqual(support["status"], "valid")
        self.assertEqual(
            support["premises"],
            ["F1", {"node": "B", "polarity": "negative"}],
        )

    def test_asserting_blocker_invalidates_dependent_conclusions(self):
        # Rebuild with only the negative path, so asserting B must collapse
        # C and the downstream D.
        self.engine.add_fact("F1")
        self.engine.add_fact("B")
        self.engine.retract_fact("B")
        self.engine.add_rules({
            "id": "RN",
            "premises": ["F1", {"node": "B", "polarity": "negative"}],
            "conclusion": "C",
        })
        self.engine.add_rules({"id": "RD", "premises": ["C"],
                               "conclusion": "D"})
        verdict = self.engine.assert_fact("B")

        self.assert_invalid("C")
        self.assert_invalid("D")
        invalidated = {item["node"]: item for item in verdict["invalidated"]}
        self.assertEqual(sorted(invalidated), ["C", "D"])
        # The lost support names the blocker as the (negative) broken
        # premise — the exception was present, not merely a missing fact.
        broken = invalidated["C"]["lost_supports"][0]["broken_premises"]
        self.assertEqual(broken, [{"node": "B", "polarity": "negative"}])
        chain = [(step["depth"], step["node"], step["cause"])
                 for step in verdict["propagation"]]
        self.assertEqual(chain, [
            (0, "C", "support-exhausted"),
            (1, "D", "support-exhausted"),
        ])

    def test_blocker_keeps_positive_supports_alive(self):
        self.build_exception_procedure()
        verdict = self.engine.assert_fact("B")
        # RN breaks, but C and D survive on the untouched positive support.
        self.assert_valid("C")
        self.assert_valid("D")
        self.assertEqual(
            [item["node"] for item in verdict["invalidated"]], [])
        live = [s["rule_id"] for s in self.conclusion("C")["supports"]
                if s["status"] == "valid"]
        self.assertEqual(live, ["R2"])

    def test_withdraw_blocker_restores_alongside_positive_supports(self):
        self.build_exception_procedure()
        self.engine.assert_fact("B")           # RN blocked, R2 keeps C
        verdict = self.engine.retract_fact("B")  # blocker withdrawn
        # RN re-fires and coexists with R2: nothing is wrongly retracted.
        self.assert_valid("C")
        self.assert_valid("D")
        live = sorted(s["rule_id"]
                      for s in self.conclusion("C")["supports"]
                      if s["status"] == "valid")
        self.assertEqual(live, ["R2", "RN"])

    def test_blocked_negative_premise_listed_on_support(self):
        self.build_exception_procedure()
        self.engine.assert_fact("B")
        support = next(s for s in self.conclusion("C")["supports"]
                       if s["rule_id"] == "RN")
        self.assertEqual(support["status"], "invalid")
        self.assertEqual(support["blocked_premises"],
                         [{"node": "B", "polarity": "negative"}])

    def test_justification_flags_confirmed_absence(self):
        self.build_exception_procedure()
        tree = self.engine.justification("C")
        rn = next(s for s in tree["supports"] if s["rule_id"] == "RN")
        blocker = next(p for p in rn["premises"]
                       if p.get("polarity") == "negative")
        self.assertEqual(blocker["node"], "B")
        self.assertTrue(blocker["absence_confirmed"])
        self.assertFalse(blocker["valid"])

        self.engine.assert_fact("B")
        tree = self.engine.justification("C")
        rn = next(s for s in tree["supports"] if s["rule_id"] == "RN")
        blocker = next(p for p in rn["premises"]
                       if p.get("polarity") == "negative")
        self.assertFalse(blocker["absence_confirmed"])
        self.assertTrue(blocker["valid"])

    def test_absence_of_a_true_conclusion_does_not_conjure(self):
        # X derives only from the absence of Y; Y is independently grounded
        # through G.  Stratification must keep X invalid.
        self.engine.add_fact("G")
        self.engine.add_rules([
            {"id": "RY", "premises": ["G"], "conclusion": "Y"},
            {"id": "RX",
             "premises": [{"node": "Y", "polarity": "negative"}],
             "conclusion": "X"},
        ])
        self.assert_valid("Y")
        self.assert_invalid("X")
        verdict = self.engine.retract_fact("G")  # Y falls -> X may rise
        self.assert_invalid("Y")
        self.assert_valid("X")
        self.assertEqual(
            [i["node"] for i in verdict["invalidated"]], ["Y"])

    def test_negative_edge_cycle_rejected_without_pollution(self):
        self.engine.add_fact("F1")
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules([
                {"id": "N1",
                 "premises": [{"node": "B", "polarity": "negative"}],
                 "conclusion": "A"},
                {"id": "N2", "premises": ["A"], "conclusion": "B"},
            ])
        state = self.engine.state()
        self.assertEqual(state["rules"], [])
        self.assertEqual(state["conclusions"], [])

    def test_negative_self_loop_rejected(self):
        with self.assertRaises(ValidationFailed):
            self.engine.add_rules({
                "id": "N3",
                "premises": [{"node": "X", "polarity": "negative"}],
                "conclusion": "X",
            })
        self.assertEqual(self.engine.state()["rules"], [])

    def test_positive_only_cycle_still_accepted_and_inert(self):
        self.engine.add_fact("F1")
        self.engine.add_rules([
            {"id": "P1", "premises": ["Y"], "conclusion": "Z"},
            {"id": "P2", "premises": ["Z"], "conclusion": "Y"},
        ])
        self.assert_invalid("Y")
        self.assert_invalid("Z")

    def test_chained_negation_propagates_both_ways(self):
        self.engine.add_fact("A")
        self.engine.retract_fact("A")
        self.engine.add_rules([
            {"id": "RB",
             "premises": [{"node": "A", "polarity": "negative"}],
             "conclusion": "B"},
            {"id": "RC",
             "premises": [{"node": "B", "polarity": "negative"}],
             "conclusion": "C"},
        ])
        self.assert_valid("B")
        self.assert_invalid("C")
        self.engine.assert_fact("A")
        self.assert_invalid("B")
        self.assert_valid("C")
        self.engine.retract_fact("A")
        self.assert_valid("B")
        self.assert_invalid("C")

    def test_negation_survives_restart_and_is_deterministic(self):
        self.build_exception_procedure()
        self.engine.assert_fact("B")  # RN blocked, C alive on R2
        self.engine.close()
        reopened = Engine(self.db)
        try:
            c = next(x for x in reopened.state()["conclusions"]
                     if x["id"] == "C")
            self.assertTrue(c["valid"])
            rn = next(s for s in c["supports"] if s["rule_id"] == "RN")
            self.assertEqual(rn["status"], "invalid")
            self.assertEqual(rn["blocked_premises"],
                             [{"node": "B", "polarity": "negative"}])
            # Withdrawing the blocker after restart restores the path.
            reopened.retract_fact("B")
            c = next(x for x in reopened.state()["conclusions"]
                     if x["id"] == "C")
            self.assertTrue(c["valid"])
            self.assertIn("RN", [s["rule_id"] for s in c["supports"]
                                 if s["status"] == "valid"])
        finally:
            reopened.close()

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

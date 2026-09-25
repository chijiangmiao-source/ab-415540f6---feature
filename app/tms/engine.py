"""Justification-based truth-maintenance engine over ground positive rules.

Semantics: a conclusion is valid iff it belongs to the *least fixed point*
of the rules over the currently asserted facts.  Consequently cyclic rules
never conjure validity out of thin air — a support loop with no ground fact
under it collapses as soon as its last external support is retracted.

Every rule firing persists its complete premise set (`supports` table), and
retraction propagates through the reverse index (`rule_premises`) inside the
same persistent transaction as the fact status flip.
"""

from __future__ import annotations

import re

from .store import Store, utcnow

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class TmsError(Exception):
    """Base error carrying an HTTP-ish status code."""

    status = 400
    code = "tms_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class ValidationFailed(TmsError):
    status = 400
    code = "validation_failed"


class NotFound(TmsError):
    status = 404
    code = "not_found"


class Conflict(TmsError):
    status = 409
    code = "conflict"


def _check_id(kind: str, value) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValidationFailed(
            f"{kind} id must match {ID_RE.pattern!r}, got {value!r}"
        )
    return value


class Engine:
    def __init__(self, db_path: str):
        self.store = Store(db_path)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------- mutations

    def add_fact(self, fact_id, label: str = "") -> dict:
        fact_id = _check_id("fact", fact_id)
        if not isinstance(label, str):
            raise ValidationFailed("label must be a string")
        with self.store.tx():
            if self.store.get_fact(fact_id) is not None:
                raise Conflict(f"fact {fact_id!r} already exists")
            if self.store.node_kind(fact_id) == "conclusion":
                raise Conflict(
                    f"{fact_id!r} is already used as a rule conclusion"
                )
            self.store.add_fact(fact_id, label)
            self.store.record_event(
                "fact_added", {"fact_id": fact_id, "label": label}
            )
        return {"fact_id": fact_id, "status": "asserted"}

    def add_rules(self, specs) -> dict:
        """Add one rule or an atomic batch of rules.

        A batch is validated as a unit: premises may reference existing
        facts, existing conclusions, or conclusions of sibling rules in the
        same batch (this is how multi-rule cycles can be declared).  Any
        failure aborts the whole transaction so the procedure is never
        polluted by a partial write.
        """
        if isinstance(specs, dict):
            specs = [specs]
        if not isinstance(specs, list) or not specs:
            raise ValidationFailed("rule spec must be an object or a "
                                   "non-empty list of objects")

        parsed = []
        seen_ids = set()
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValidationFailed("each rule must be an object")
            rule_id = _check_id("rule", spec.get("id"))
            if rule_id in seen_ids:
                raise ValidationFailed(
                    f"duplicate rule id {rule_id!r} in batch"
                )
            seen_ids.add(rule_id)
            conclusion = _check_id("conclusion", spec.get("conclusion"))
            premises = spec.get("premises")
            if not isinstance(premises, list) or not premises:
                raise ValidationFailed(
                    f"rule {rule_id!r}: premises must be a non-empty list"
                )
            normalised = []
            for premise in premises:
                premise = _check_id("premise", premise)
                if premise not in normalised:
                    normalised.append(premise)
            if conclusion in normalised:
                raise ValidationFailed(
                    f"rule {rule_id!r}: self-supporting loop rejected"
                    f" ({conclusion!r} supports itself)"
                )
            parsed.append((rule_id, normalised, conclusion))

        with self.store.tx():
            known_nodes = self._known_nodes()
            known_nodes |= {conclusion for _, _, conclusion in parsed}
            for rule_id, premises, conclusion in parsed:
                if self.store.get_rule(rule_id) is not None:
                    raise Conflict(f"rule {rule_id!r} already exists")
                if self.store.node_kind(conclusion) == "fact":
                    raise Conflict(
                        f"rule {rule_id!r}: conclusion {conclusion!r}"
                        f" collides with an existing fact"
                    )
                for premise in premises:
                    if premise not in known_nodes:
                        raise ValidationFailed(
                            f"rule {rule_id!r}: premise {premise!r} refers"
                            f" to an unknown fact or conclusion"
                        )

            added = []
            for rule_id, premises, conclusion in parsed:
                self.store.add_rule(rule_id, premises, conclusion)
                if self.store.node_kind(conclusion) is None:
                    self.store.set_node_state(conclusion, "conclusion", False)
                # Persist the (initially non-firing) support with its
                # complete premise set; _recompute flips it if it fires.
                self.store.upsert_support(
                    rule_id, conclusion, premises, firing=False
                )
                added.append(
                    {"id": rule_id, "premises": premises,
                     "conclusion": conclusion}
                )

            derived = []
            for conclusion in {conclusion for _, _, conclusion in parsed}:
                newly_valid, _ = self._recompute([conclusion])
                derived.extend(newly_valid)
            self.store.record_event(
                "rules_added", {"rules": added, "derived": sorted(set(derived))}
            )
        return {"added": added, "derived": sorted(set(derived))}

    def retract_fact(self, fact_id) -> dict:
        fact_id = _check_id("fact", fact_id)
        with self.store.tx():
            fact = self.store.get_fact(fact_id)
            if fact is None:
                raise NotFound(f"unknown fact {fact_id!r}")
            if fact["status"] == "retracted":
                # Idempotent: replay the verdict produced by the first
                # retraction, byte for byte.
                verdict = self._stored_verdict(fact)
                verdict["replayed"] = True
                return verdict

            before = self._candidate_snapshot(fact_id)
            self.store.set_fact_status(fact_id, "retracted")
            newly_valid, newly_invalid = self._recompute([fact_id])
            verdict = self._build_retraction_verdict(
                fact_id, before, newly_invalid
            )
            self.store.set_fact_verdict(fact_id, verdict)
            self.store.record_event("fact_retracted", verdict)
            verdict["replayed"] = False
            return verdict

    def assert_fact(self, fact_id) -> dict:
        fact_id = _check_id("fact", fact_id)
        with self.store.tx():
            fact = self.store.get_fact(fact_id)
            if fact is None:
                raise NotFound(f"unknown fact {fact_id!r}")
            if fact["status"] == "asserted":
                return {"fact_id": fact_id, "verdict": "asserted",
                        "restored": [], "replayed": True}
            self.store.set_fact_status(fact_id, "asserted")
            newly_valid, _ = self._recompute([fact_id])
            verdict = {
                "fact_id": fact_id,
                "verdict": "asserted",
                "restored": sorted(newly_valid),
                "at": utcnow(),
            }
            self.store.record_event("fact_asserted", verdict)
            verdict["replayed"] = False
            return verdict

    # --------------------------------------------------------------- queries

    def state(self) -> dict:
        with self.store.lock:
            facts = [
                {
                    "id": row["id"],
                    "label": row["label"],
                    "status": row["status"],
                    "valid": row["status"] == "asserted",
                }
                for row in self.store.list_facts()
            ]
            rules = []
            for rule in self.store.list_rules():
                support = self.store.get_support(rule["id"])
                rules.append({
                    "id": rule["id"],
                    "premises": rule["premises"],
                    "conclusion": rule["conclusion"],
                    "firing": bool(support and support["status"] == "valid"),
                })
            conclusions = []
            for node in self.store.list_conclusions():
                conclusions.append({
                    "id": node["id"],
                    "valid": node["valid"],
                    "supports": self.store.supports_for_conclusion(node["id"]),
                })
            return {
                "facts": facts,
                "rules": rules,
                "conclusions": conclusions,
                "retracted_facts": [f["id"] for f in facts
                                    if f["status"] == "retracted"],
                "last_event": self.store.last_event(),
            }

    def justification(self, node) -> dict:
        """Complete current basis of a node, recursively expanded."""
        node = _check_id("node", node)
        with self.store.lock:
            if self.store.node_kind(node) is None:
                raise NotFound(f"unknown node {node!r}")
            return self._justify(node, path=())

    def health(self) -> dict:
        return {"status": "ok" if self.store.ping() else "degraded"}

    def reset(self) -> None:
        with self.store.tx():
            self.store.reset()
            self.store.record_event("reset", {})

    # -------------------------------------------------------------- internals

    def _known_nodes(self) -> set:
        nodes = {row["id"] for row in self.store.list_facts()}
        nodes |= {rule["conclusion"] for rule in self.store.list_rules()}
        return nodes

    def _stored_verdict(self, fact_row) -> dict:
        import json
        if fact_row["last_verdict"]:
            return json.loads(fact_row["last_verdict"])
        # Fact retracted before verdicts were recorded (should not happen).
        return {"fact_id": fact_row["id"], "verdict": "retracted",
                "invalidated": [], "retained": [], "propagation": [],
                "at": fact_row["updated_at"]}

    def _candidate_snapshot(self, *seeds) -> dict:
        """Validity + supports of every conclusion downstream of `seeds`."""
        candidates = self._candidates(seeds)
        snapshot = {}
        for node in candidates:
            snapshot[node] = {
                "valid": self.store.node_valid(node),
                "supports": self.store.supports_for_conclusion(node),
            }
        return snapshot

    def _candidates(self, seeds) -> set:
        """Conclusions reachable from `seeds` via the reverse index."""
        candidates = set()
        stack = list(seeds)
        while stack:
            node = stack.pop()
            for rule in self.store.rules_triggered_by(node):
                conclusion = rule["conclusion"]
                if conclusion not in candidates:
                    candidates.add(conclusion)
                    stack.append(conclusion)
        return candidates

    def _recompute(self, seeds):
        """Propagate a change from `seeds` through the reverse index.

        Only conclusions downstream of the seeds can change validity; their
        new validity is the least fixed point over the candidate subgraph
        with out-of-candidate premises pinned to their stored validity.
        Returns (newly_valid, newly_invalid).
        """
        candidates = self._candidates(seeds)
        for seed in seeds:
            if self.store.node_kind(seed) == "conclusion":
                candidates.add(seed)
        if not candidates:
            return [], []

        rules = [r for r in self.store.list_rules()
                 if r["conclusion"] in candidates]

        def premise_ok(premise, valid_set):
            if premise in candidates:
                return premise in valid_set
            return self.store.node_valid(premise)

        valid_set = set()
        changed = True
        while changed:
            changed = False
            for rule in rules:
                conclusion = rule["conclusion"]
                if conclusion in valid_set:
                    continue
                if all(premise_ok(p, valid_set) for p in rule["premises"]):
                    valid_set.add(conclusion)
                    changed = True

        newly_valid, newly_invalid = [], []
        for node in sorted(candidates):
            old = self.store.node_valid(node)
            new = node in valid_set
            if old != new:
                self.store.set_node_state(node, "conclusion", new)
                (newly_valid if new else newly_invalid).append(node)

        # Persist firing state (with complete premise sets) for every rule
        # whose conclusion could have changed.
        for rule in rules:
            firing = all(
                (p in valid_set) if p in candidates
                else self.store.node_valid(p)
                for p in rule["premises"]
            )
            self.store.upsert_support(
                rule["id"], rule["conclusion"], rule["premises"], firing
            )
        return newly_valid, newly_invalid

    def _build_retraction_verdict(self, fact_id, before, newly_invalid):
        newly_invalid = set(newly_invalid)
        invalidated = []
        for node in sorted(newly_invalid):
            lost = []
            for support in before.get(node, {}).get("supports", []):
                if support["status"] == "valid":
                    broken = [p for p in support["premises"]
                              if not self.store.node_valid(p)]
                    lost.append({**support, "broken_premises": broken})
            invalidated.append({"node": node, "lost_supports": lost})

        retained = []
        for node, prior in sorted(before.items()):
            if node in newly_invalid or not prior["valid"]:
                continue
            remaining = [
                {"rule_id": s["rule_id"], "premises": s["premises"]}
                for s in self.store.supports_for_conclusion(node)
                if s["status"] == "valid"
            ]
            retained.append({"node": node, "remaining_supports": remaining})

        propagation = self._propagation_chain(newly_invalid)
        return {
            "fact_id": fact_id,
            "verdict": "retracted",
            "at": utcnow(),
            "invalidated": invalidated,
            "retained": retained,
            "propagation": propagation,
        }

    def _propagation_chain(self, newly_invalid) -> list:
        """Order newly invalidated conclusions into causal waves.

        A node joins the chain once every one of its supports has a premise
        that is already known invalid — i.e. its support is genuinely
        exhausted.  Cyclic residue (mutually supporting loops) is emitted as
        a final wave flagged `cyclic`.
        """
        remaining = set(newly_invalid)
        # Seed with everything invalid *before* this retraction (including
        # the just-retracted fact); newly invalidated nodes join wave by
        # wave so the chain reflects the causal order of exhaustion.
        invalid = self.store.invalid_nodes() - remaining
        chain = []
        depth = 0
        while remaining:
            wave = []
            for node in sorted(remaining):
                supports = self.store.supports_for_conclusion(node)
                if supports and all(
                    any(p in invalid for p in s["premises"])
                    for s in supports
                ):
                    wave.append(node)
            if not wave:  # cyclic residue: no well-founded ordering exists
                wave = sorted(remaining)
                for node in wave:
                    chain.append({"depth": depth, "node": node,
                                  "cause": "cyclic-support-collapsed"})
                break
            for node in wave:
                chain.append({"depth": depth, "node": node,
                              "cause": "support-exhausted"})
            invalid |= set(wave)
            remaining -= set(wave)
            depth += 1
        return chain

    def _justify(self, node, path) -> dict:
        kind = self.store.node_kind(node)
        if kind == "fact":
            fact = self.store.get_fact(node)
            return {"node": node, "kind": "fact", "label": fact["label"],
                    "status": fact["status"],
                    "valid": fact["status"] == "asserted"}
        if node in path:
            return {"node": node, "kind": "conclusion", "cyclic": True,
                    "valid": self.store.node_valid(node)}
        supports = []
        for support in self.store.supports_for_conclusion(node):
            supports.append({
                "rule_id": support["rule_id"],
                "status": support["status"],
                "premises": [self._justify(p, path + (node,))
                             for p in support["premises"]],
            })
        return {"node": node, "kind": "conclusion",
                "valid": self.store.node_valid(node),
                "supports": supports}

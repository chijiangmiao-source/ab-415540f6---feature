"""Justification-based truth-maintenance engine over ground rules.

Rules carry *polarised* premises: a positive premise must hold, a negative
premise must be absent — the safety officer's "release only when some raw
fact or conclusion does NOT hold" exception.

Semantics: a conclusion is valid iff it belongs to the stratified least
fixed point of the rules over the currently asserted facts.  Rules are
accepted only when the conclusion dependency graph is *stratified*: a cycle
is allowed while every edge on it is positive (a positive loop still
derives nothing without a ground fact under it), but any cycle containing a
negative edge is rejected, so negation never oscillates or conjures
validity.  Consequently cyclic rules never conjure validity out of thin
air — a support loop with no ground fact under it collapses as soon as its
last external support is retracted.

Every rule firing persists its complete polarised premise set (`supports`
table), and retraction/assertion propagates through the reverse index
(`rule_premises`) inside the same persistent transaction as the fact
status flip.  Asserting a previously absent blocking fact invalidates the
conclusions that relied on its absence and everything downstream;
withdrawing it restores them alongside the untouched positive supports.
"""

from __future__ import annotations

import re

from .store import Store, utcnow

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
POLARITIES = ("positive", "negative")


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
            seen_pairs = set()
            polarity_by_node = {}
            for premise in premises:
                node, polarity = self._parse_premise(rule_id, premise)
                prior = polarity_by_node.get(node)
                if prior is not None and prior != polarity:
                    # The node would have to be both present and absent.
                    raise ValidationFailed(
                        f"rule {rule_id!r}: premise {node!r} required both"
                        f" present and absent"
                    )
                polarity_by_node[node] = polarity
                if (node, polarity) not in seen_pairs:
                    seen_pairs.add((node, polarity))
                    normalised.append((node, polarity))
            premise_nodes = set(polarity_by_node)
            if conclusion in premise_nodes:
                # A ⇒ A is never stratified: positive self-support is a
                # groundless loop, a negative one is a negation cycle.
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
                for premise, _polarity in premises:
                    if premise not in known_nodes:
                        raise ValidationFailed(
                            f"rule {rule_id!r}: premise {premise!r} refers"
                            f" to an unknown fact or conclusion"
                        )

            # Reject any cycle carrying a negative edge (unstratified
            # negation); positive-only cycles stay inert until grounded.
            self._assert_stratified(parsed)

            added = []
            for rule_id, premises, conclusion in parsed:
                self.store.add_rule(rule_id, premises, conclusion)
                if self.store.node_kind(conclusion) is None:
                    self.store.set_node_state(conclusion, "conclusion", False)
                # Persist the (initially non-firing) support with its
                # complete polarised premise set; _recompute flips it.
                self.store.upsert_support(
                    rule_id, conclusion, premises, firing=False
                )
                added.append({
                    "id": rule_id,
                    "premises": [self._premise_out(p) for p in premises],
                    "conclusion": conclusion,
                })

            derived = []
            new_conclusions = {conclusion
                               for _, _, conclusion in parsed}
            newly_valid, _ = self._recompute(sorted(new_conclusions))
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
                        "restored": [], "invalidated": [],
                        "propagation": [], "replayed": True}
            # Snapshot downstream before the flip: asserting a previously
            # absent blocker must invalidate conclusions that relied on its
            # absence (and everything downstream of them).
            before = self._candidate_snapshot(fact_id)
            self.store.set_fact_status(fact_id, "asserted")
            newly_valid, newly_invalid = self._recompute([fact_id])
            verdict = {
                "fact_id": fact_id,
                "verdict": "asserted",
                "restored": sorted(newly_valid),
                "invalidated": self._invalidated_detail(before, newly_invalid),
                "propagation": self._propagation_chain(newly_invalid),
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
                    "premises": [self._premise_out(p)
                                 for p in rule["premises"]],
                    "conclusion": rule["conclusion"],
                    "firing": bool(support and support["status"] == "valid"),
                })
            conclusions = []
            for node in self.store.list_conclusions():
                supports = []
                for s in self.store.supports_for_conclusion(node["id"]):
                    out = self._support_out(s)
                    # Only polarised supports carry extra detail, so the
                    # responses of an all-positive procedure stay byte-for
                    # -byte identical to the original.
                    if s["status"] != "valid" and \
                            any(pol == "negative" for _, pol in s["premises"]):
                        blocked = self._broken_premises(s)
                        if blocked:
                            out["blocked_premises"] = blocked
                    supports.append(out)
                conclusions.append({
                    "id": node["id"],
                    "valid": node["valid"],
                    "supports": supports,
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

    @staticmethod
    def _parse_premise(rule_id, premise):
        """Normalise one premise entry to a (node, polarity) pair.

        A bare id string is a positive premise; an object carries an
        explicit polarity, e.g. {"node": "B", "polarity": "negative"}.
        """
        if isinstance(premise, str):
            return _check_id("premise", premise), "positive"
        if isinstance(premise, dict):
            node = _check_id("premise", premise.get("node"))
            polarity = premise.get("polarity", "positive")
            if polarity not in POLARITIES:
                raise ValidationFailed(
                    f"rule {rule_id!r}: premise {node!r} has unknown"
                    f" polarity {polarity!r}"
                )
            return node, polarity
        raise ValidationFailed(
            f"rule {rule_id!r}: premise must be an id string or an object"
            f" with node/polarity, got {premise!r}"
        )

    @staticmethod
    def _premise_out(entry):
        """API shape: positive premises stay bare ids (the original
        all-positive responses are unchanged), negative premises are
        tagged objects."""
        node, polarity = entry
        if polarity == "positive":
            return node
        return {"node": node, "polarity": "negative"}

    def _support_out(self, support) -> dict:
        """External shape of a stored support: positive premises remain bare
        ids so all-positive procedures keep their original responses; only
        negative premises are tagged."""
        out = dict(support)
        out["premises"] = [self._premise_out(p) for p in support["premises"]]
        return out

    def _broken_premises(self, support) -> list:
        """Premises of a stored support that currently fail it:
        positive premises that are absent, negative premises whose blocker
        is present."""
        broken = []
        for node, polarity in support["premises"]:
            holds = self.store.node_valid(node)
            satisfied = holds if polarity == "positive" else not holds
            if not satisfied:
                broken.append(self._premise_out((node, polarity)))
        return broken

    def _strata(self) -> dict:
        """Stratification level of every node: level[v] is the length of the
        longest weighted path into v, where a negative edge weighs 1 and a
        positive edge 0.  Accepted rule sets are stratified, so the
        relaxation always settles (a negative edge always climbs a level).
        Facts and otherwise-unreferenced nodes sit at level 0.
        """
        rules = self.store.list_rules()
        vertices = {row["id"] for row in self.store.list_facts()}
        edges = []
        for rule in rules:
            conclusion = rule["conclusion"]
            vertices.add(conclusion)
            for node, polarity in rule["premises"]:
                vertices.add(node)
                edges.append((node, conclusion,
                              1 if polarity == "negative" else 0))
        level = {node: 0 for node in vertices}
        changed = True
        while changed:
            changed = False
            for src, dst, weight in edges:
                candidate = level[src] + weight
                if candidate > level[dst]:
                    level[dst] = candidate
                    changed = True
        return level

    def _assert_stratified(self, parsed) -> None:
        """Reject cycles that carry a negative dependency edge.

        Edge premise -> conclusion has weight 0 (positive) or 1 (negative).
        A stratification level must satisfy level[v] >= level[u] + w.  A
        positive-only cycle has weight 0 and stays bounded; any cycle
        containing a negative edge has weight >= 1 and is unsatisfiable,
        which longest-path relaxation detects as an unending increase.
        """
        rules = self.store.list_rules()
        vertices = {row["id"] for row in self.store.list_facts()}
        edges = []
        for rule in rules:
            conclusion = rule["conclusion"]
            vertices.add(conclusion)
            for node, polarity in rule["premises"]:
                vertices.add(node)
                edges.append((node, conclusion,
                              1 if polarity == "negative" else 0))
        for _, premises, conclusion in parsed:
            vertices.add(conclusion)
            for node, polarity in premises:
                vertices.add(node)
                edges.append((node, conclusion,
                              1 if polarity == "negative" else 0))

        level = {node: 0 for node in vertices}
        for _ in range(len(vertices)):
            changed = False
            for src, dst, weight in edges:
                candidate = level[src] + weight
                if candidate > level[dst]:
                    level[dst] = candidate
                    changed = True
            if not changed:
                return
        raise ValidationFailed(
            "rule rejected: a cycle containing a negative (absence)"
            " premise is not stratified and has no stable truth assignment"
        )

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
        """Conclusions reachable from `seeds` via a premise of either polarity.

        Both a positive premise (its becoming valid can enable a rule) and a
        negative premise (its becoming valid blocks a rule; its becoming
        invalid re-enables one) propagate changes to the rule's conclusion.
        """
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
        """Recompute validity after a change at `seeds`.

        The whole model is evaluated level by level (`_strata`): each level
        closes only under positive premises, while every negative premise of
        a level-L rule belongs to a strictly lower, already-final level — so
        absence is observed against a settled truth value and can never be
        guessed early or oscillate.  Persistence is still confined to the
        conclusions reachable from `seeds`: only those node states flip and
        only their supports are rewritten, keeping change reports precise.
        Returns (newly_valid, newly_invalid).
        """
        candidates = self._candidates(seeds)
        for seed in seeds:
            if self.store.node_kind(seed) == "conclusion":
                candidates.add(seed)

        strata = self._strata()
        rules = self.store.list_rules()
        by_level = {}
        for rule in rules:
            by_level.setdefault(strata.get(rule["conclusion"], 0), []) \
                .append(rule)

        valid = {row["id"] for row in self.store.list_facts()
                 if row["status"] == "asserted"}

        def holds(entry):
            node, polarity = entry
            is_valid = node in valid
            return is_valid if polarity == "positive" else not is_valid

        for level in sorted(by_level):
            level_rules = by_level[level]
            changed = True
            while changed:  # positive-only least fixed point within a level
                changed = False
                for rule in level_rules:
                    conclusion = rule["conclusion"]
                    if conclusion in valid:
                        continue
                    if all(holds(p) for p in rule["premises"]):
                        valid.add(conclusion)
                        changed = True

        if not candidates:
            return [], []

        newly_valid, newly_invalid = [], []
        for node in sorted(candidates):
            old = self.store.node_valid(node)
            new = node in valid
            if old != new:
                self.store.set_node_state(node, "conclusion", new)
                (newly_valid if new else newly_invalid).append(node)

        # Persist firing state (with complete polarised premise sets) for
        # every rule whose conclusion could have changed.
        for rule in rules:
            if rule["conclusion"] not in candidates:
                continue
            firing = all(holds(p) for p in rule["premises"])
            self.store.upsert_support(
                rule["id"], rule["conclusion"], rule["premises"], firing
            )
        return newly_valid, newly_invalid

    def _invalidated_detail(self, before, newly_invalid) -> list:
        """Per-node supports lost by this change, with the failing premises
        (missing positive premises or newly present blockers)."""
        invalidated = []
        for node in sorted(set(newly_invalid)):
            lost = []
            for support in before.get(node, {}).get("supports", []):
                if support["status"] == "valid":
                    entry = self._support_out(support)
                    entry["broken_premises"] = self._broken_premises(support)
                    lost.append(entry)
            invalidated.append({"node": node, "lost_supports": lost})
        return invalidated

    def _build_retraction_verdict(self, fact_id, before, newly_invalid):
        newly_invalid = set(newly_invalid)
        invalidated = self._invalidated_detail(before, newly_invalid)

        retained = []
        for node, prior in sorted(before.items()):
            if node in newly_invalid or not prior["valid"]:
                continue
            remaining = [
                {"rule_id": s["rule_id"],
                 "premises": [self._premise_out(p) for p in s["premises"]]}
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
        that currently fails it — a positive premise already known invalid,
        or a negative premise whose blocking fact is present — i.e. its
        support is genuinely exhausted.  Cyclic residue (mutually supporting
        loops) is emitted as a final wave flagged `cyclic`.
        """
        remaining = set(newly_invalid)
        # Seed with everything invalid *before* this change (including the
        # just-retracted fact); newly invalidated nodes join wave by wave so
        # the chain reflects the causal order of exhaustion.
        invalid = self.store.invalid_nodes() - remaining
        chain = []
        depth = 0

        def support_exhausted(support):
            for node, polarity in support["premises"]:
                if polarity == "positive":
                    if node in invalid:
                        return True
                elif self.store.node_valid(node):
                    # Negative premise: the blocking fact is present, so the
                    # "absence" requirement fails.
                    return True
            return False

        while remaining:
            wave = []
            for node in sorted(remaining):
                supports = self.store.supports_for_conclusion(node)
                if supports and all(support_exhausted(s) for s in supports):
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
            premises = []
            for premise, polarity in support["premises"]:
                child = self._justify(premise, path + (node,))
                if polarity == "negative":
                    # Surface the absence requirement explicitly: the
                    # premise is satisfied exactly when the blocker is
                    # confirmed invalid.
                    child = {**child, "polarity": "negative",
                             "absence_confirmed": not child["valid"]}
                premises.append(child)
            supports.append({
                "rule_id": support["rule_id"],
                "status": support["status"],
                "premises": premises,
            })
        return {"node": node, "kind": "conclusion",
                "valid": self.store.node_valid(node),
                "supports": supports}

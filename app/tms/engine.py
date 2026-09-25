"""Justification-based truth-maintenance engine over ground rules.

Rules are ground (variable-free) and forward-chaining, but each premise
carries a *polarity*:

- a positive premise must hold (the node is valid);
- a negative premise expresses an exception — it is satisfied by the
  *absence* of the referenced node (the node is invalid/unknown).  This is
  negation as absence, so a rule can read "release only when the blocking
  fact has *not* been asserted".

Semantics: a conclusion is valid iff it belongs to the *least fixed point*
of the rules over the currently asserted facts under stratified negation.
Consequently cyclic rules never conjure validity out of thin air, and rules
whose dependency graph contains a cycle *through a negative edge* are
rejected at submission time — such a program has no well-founded layering.
Asserting the formerly absent blocker invalidates everything that relied on
its absence; withdrawing the blocker restores the conclusions whose other
premises still hold, alongside their pre-existing positive supports.

Every rule firing persists its complete premise set with polarities
(`supports` table), and retraction propagates through the reverse index
(`rule_premises`) inside the same persistent transaction as the fact status
flip.
"""

from __future__ import annotations

import re

from .store import Store, utcnow

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
POLARITIES = ("pos", "neg")


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


def _parse_premise(raw):
    """Normalise a premise spec to a (node, polarity) pair.

    Accepted forms: a plain node id string (positive), or an object
    {"id": ..., "polarity": "pos"|"neg"} ("positive"/"negative" also
    accepted).  A negative premise is the exception condition of the
    procedure: it is satisfied exactly while the node is absent.
    """
    if isinstance(raw, dict):
        node = _check_id("premise", raw.get("id"))
        polarity = raw.get("polarity", "pos")
        if not isinstance(polarity, str):
            raise ValidationFailed(
                f"premise polarity must be a string, got {polarity!r}"
            )
        polarity = {"positive": "pos", "negative": "neg"}.get(
            polarity, polarity)
        if polarity not in POLARITIES:
            raise ValidationFailed(
                f"premise polarity must be one of {POLARITIES},"
                f" got {polarity!r}"
            )
        return node, polarity
    return _check_id("premise", raw), "pos"


def _wire_premise(premise: str, polarity: str):
    """Wire form of a premise: plain string for positive premises (the
    historical shape), an object only when the premise is negated."""
    if polarity == "neg":
        return {"id": premise, "polarity": "neg"}
    return premise


def _wire_premises(premises) -> list:
    return [_wire_premise(p, pol) for p, pol in premises]


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

        Premises carry polarity.  Positive cycles are legal (they simply
        derive nothing until grounded); a dependency cycle that passes
        through a *negative* edge is rejected, because stratified negation
        has no well-founded layering for it.
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
            polarities = {}
            for premise in premises:
                node, polarity = _parse_premise(premise)
                if node in polarities:
                    if polarities[node] != polarity:
                        raise ValidationFailed(
                            f"rule {rule_id!r}: premise {node!r} listed"
                            f" both positively and negatively"
                        )
                    continue
                polarities[node] = polarity
                normalised.append((node, polarity))
            if conclusion in polarities:
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
            self._check_negative_cycles(parsed)

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
                    {"id": rule_id, "premises": _wire_premises(premises),
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

    def _check_negative_cycles(self, parsed) -> None:
        """Reject dependency cycles that pass through a negative edge.

        Nodes are facts/conclusions; every premise contributes a directed
        edge conclusion -> premise (a dependency).  A cycle containing at
        least one negative edge makes stratified evaluation impossible, so
        the batch is refused before anything is persisted.  Purely positive
        cycles remain legal.
        """
        edges = {}

        def add_edge(source, target, polarity):
            edges.setdefault(source, []).append((target, polarity))

        for rule in self.store.list_rules():
            for premise, polarity in rule["premises"]:
                add_edge(rule["conclusion"], premise, polarity)
        for _rule_id, premises, conclusion in parsed:
            for premise, polarity in premises:
                add_edge(conclusion, premise, polarity)

        # For every negative edge u -> v, check whether v can still reach u
        # through the dependency graph; if so the edge closes a cycle that
        # includes a negative edge.
        for source, targets in edges.items():
            for target, polarity in targets:
                if polarity != "neg":
                    continue
                if self._reaches(edges, target, source):
                    raise ValidationFailed(
                        f"negative premise {target!r} of conclusion"
                        f" {source!r} closes a dependency cycle through"
                        f" negation; stratified evaluation impossible"
                    )

    @staticmethod
    def _reaches(edges, start, goal) -> bool:
        stack, seen = [start], set()
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(target for target, _ in edges.get(node, ()))
        return False

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
                fact_id, before, newly_valid, newly_invalid
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
            before = self._candidate_snapshot(fact_id)
            self.store.set_fact_status(fact_id, "asserted")
            newly_valid, newly_invalid = self._recompute([fact_id])
            verdict = {
                "fact_id": fact_id,
                "verdict": "asserted",
                "restored": sorted(newly_valid),
                "at": utcnow(),
            }
            if newly_invalid:
                # Asserting a blocking fact knocks out every conclusion
                # that relied on its absence, plus their downstream.
                verdict["invalidated"] = self._invalidated_details(
                    before, newly_invalid
                )
                verdict["propagation"] = self._propagation_chain(newly_invalid)
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
                entry = {
                    "id": rule["id"],
                    "premises": _wire_premises(rule["premises"]),
                    "conclusion": rule["conclusion"],
                    "firing": bool(support and support["status"] == "valid"),
                }
                blocked = [p for p, pol in rule["premises"]
                           if pol == "neg" and self.store.node_valid(p)]
                if blocked:
                    # Negative premises currently present, hence blocking.
                    entry["blocked_by"] = sorted(blocked)
                rules.append(entry)
            conclusions = []
            for node in self.store.list_conclusions():
                conclusions.append({
                    "id": node["id"],
                    "valid": node["valid"],
                    "supports": [
                        self._wire_support(s)
                        for s in self.store.supports_for_conclusion(node["id"])
                    ],
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
        Because cycles through negation are rejected at submission time,
        the candidate subgraph is stratifiable: it is evaluated one strongly
        connected component at a time (dependencies first), so a negative
        premise is always read after its node has reached its final value.
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

        def premise_met(premise, polarity, valid_set):
            if premise in candidates:
                value = premise in valid_set
            else:
                value = self.store.node_valid(premise)
            return value if polarity == "pos" else not value

        valid_set = set()
        for stratum in self._stratify(candidates, rules):
            members = set(stratum)
            changed = True
            while changed:
                changed = False
                for rule in rules:
                    conclusion = rule["conclusion"]
                    if conclusion in members and conclusion not in valid_set:
                        if all(premise_met(p, pol, valid_set)
                               for p, pol in rule["premises"]):
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
                premise_met(p, pol, valid_set) for p, pol in rule["premises"]
            )
            self.store.upsert_support(
                rule["id"], rule["conclusion"], rule["premises"], firing
            )
        return newly_valid, newly_invalid

    @staticmethod
    def _stratify(candidates, rules):
        """Strongly connected components of the candidate dependency
        subgraph, emitted dependencies-first (Tarjan order).

        Every negative edge points from a later stratum into an earlier
        one — a negative edge inside a component would be a cycle through
        negation, which submission rejects — so each component is a purely
        positive least-fixed-point problem.
        """
        deps = {node: [] for node in candidates}
        for rule in rules:
            for premise, _polarity in rule["premises"]:
                if premise in candidates:
                    deps[rule["conclusion"]].append(premise)

        index, lowlink, on_stack, stack, strata = {}, {}, set(), [], []
        counter = 0
        for root in sorted(deps):
            if root in index:
                continue
            index[root] = lowlink[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            work = [(root, iter(sorted(deps[root])))]
            while work:
                node, neighbours = work[-1]
                descended = False
                for nxt in neighbours:
                    if nxt not in index:
                        index[nxt] = lowlink[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, iter(sorted(deps[nxt]))))
                        descended = True
                        break
                    if nxt in on_stack:
                        lowlink[node] = min(lowlink[node], index[nxt])
                if descended:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
                if lowlink[node] == index[node]:
                    stratum = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        stratum.append(member)
                        if member == node:
                            break
                    strata.append(sorted(stratum))
        return strata

    @staticmethod
    def _wire_support(support) -> dict:
        return {
            "rule_id": support["rule_id"],
            "conclusion": support["conclusion"],
            "premises": _wire_premises(support["premises"]),
            "status": support["status"],
            "fire_count": support["fire_count"],
        }

    def _premise_met_now(self, premise, polarity) -> bool:
        valid = self.store.node_valid(premise)
        return valid if polarity == "pos" else not valid

    def _invalidated_details(self, before, newly_invalid) -> list:
        newly_invalid = set(newly_invalid)
        invalidated = []
        for node in sorted(newly_invalid):
            lost = []
            for support in before.get(node, {}).get("supports", []):
                if support["status"] == "valid":
                    broken = [
                        _wire_premise(p, pol)
                        for p, pol in support["premises"]
                        if not self._premise_met_now(p, pol)
                    ]
                    lost.append({
                        "rule_id": support["rule_id"],
                        "conclusion": support["conclusion"],
                        "premises": _wire_premises(support["premises"]),
                        "status": support["status"],
                        "fire_count": support["fire_count"],
                        "broken_premises": broken,
                    })
            invalidated.append({"node": node, "lost_supports": lost})
        return invalidated

    def _build_retraction_verdict(self, fact_id, before, newly_valid,
                                  newly_invalid):
        newly_invalid = set(newly_invalid)
        retained = []
        for node, prior in sorted(before.items()):
            if node in newly_invalid or not prior["valid"]:
                continue
            remaining = [
                {"rule_id": s["rule_id"],
                 "premises": _wire_premises(s["premises"])}
                for s in self.store.supports_for_conclusion(node)
                if s["status"] == "valid"
            ]
            retained.append({"node": node, "remaining_supports": remaining})

        verdict = {
            "fact_id": fact_id,
            "verdict": "retracted",
            "at": utcnow(),
            "invalidated": self._invalidated_details(before, newly_invalid),
            "retained": retained,
            "propagation": self._propagation_chain(newly_invalid),
        }
        if newly_valid:
            # Withdrawing a blocking fact can revive conclusions whose
            # negative premises are satisfied again.
            verdict["restored"] = sorted(newly_valid)
        return verdict

    def _propagation_chain(self, newly_invalid) -> list:
        """Order newly invalidated conclusions into causal waves.

        A node joins the chain once every one of its supports is genuinely
        exhausted: each has a positive premise already known invalid, or a
        negative premise whose blocker is present.  Cyclic residue
        (mutually supporting loops) is emitted as a final wave flagged
        `cyclic`.
        """
        remaining = set(newly_invalid)
        # Seed with everything invalid *before* this change (including a
        # just-retracted fact); newly invalidated nodes join wave by wave
        # so the chain reflects the causal order of exhaustion.  Present
        # blockers need no seeding: a valid node explains a broken negative
        # premise directly.
        invalid = self.store.invalid_nodes() - remaining
        chain = []
        depth = 0
        while remaining:
            wave = []
            for node in sorted(remaining):
                supports = self.store.supports_for_conclusion(node)
                if supports and all(
                    self._support_defeated(s, invalid) for s in supports
                ):
                    wave.append(node)
            if not wave:  # cyclic residue: no well-founded ordering exists
                wave = sorted(remaining)
                for node in wave:
                    chain.append({"depth": depth, "node": node,
                                  "cause": "cyclic-support-collapsed"})
                break
            for node in wave:
                blockers = self._present_blockers(node)
                step = {"depth": depth, "node": node}
                if blockers:
                    step["cause"] = "negative-premise-blocked"
                    step["blocked_by"] = blockers
                else:
                    step["cause"] = "support-exhausted"
                chain.append(step)
            invalid |= set(wave)
            remaining -= set(wave)
            depth += 1
        return chain

    def _support_defeated(self, support, known_invalid) -> bool:
        """True when the support has at least one definitively unmet
        premise: a positive premise known invalid, or a negative premise
        whose node is present (valid) right now."""
        for premise, polarity in support["premises"]:
            if polarity == "neg":
                if self.store.node_valid(premise):
                    return True
            elif premise in known_invalid:
                return True
        return False

    def _present_blockers(self, node) -> list:
        blockers = set()
        for support in self.store.supports_for_conclusion(node):
            for premise, polarity in support["premises"]:
                if polarity == "neg" and self.store.node_valid(premise):
                    blockers.add(premise)
        return sorted(blockers)

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
                if polarity == "neg":
                    # Exception condition: satisfied exactly while the
                    # node is absent — surface both sides of that check.
                    child["polarity"] = "neg"
                    child["premise_met"] = not child["valid"]
                premises.append(child)
            supports.append({
                "rule_id": support["rule_id"],
                "status": support["status"],
                "premises": premises,
            })
        return {"node": node, "kind": "conclusion",
                "valid": self.store.node_valid(node),
                "supports": supports}

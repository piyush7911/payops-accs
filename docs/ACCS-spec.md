Applied Research — Verified Long-Running Agents & Consistency Scoring

Sprint slice: make a long-running agent provably obey a declarative policy on every infinite trace, and produce a calibrated factual-consistency score for its outputs.

1. Sprint Objective
An agent that runs indefinitely and gates its own actions against a policy is only trustworthy if that gating is sound over the whole run — a property about infinite behaviour, which is coinductive rather than inductive. Alongside soundness, outputs need a calibrated consistency score. This sprint puts both on a formal footing and ships them as components. Each piece is independently useful: a verification result and a scorer.

2. System Scope
Working name: Agent Compliance & Consistency Suite (ACCS).
In scope:
	•	A model of an agent's gate decisions and a proof they satisfy a declarative policy on all infinite traces.
	•	A calibrated factual-consistency score for agent outputs, with provenance, usable as a penalty signal by an external optimizer.
Out of scope: the policy's content (supplied as a declarative spec) and what any external optimizer does with the score. ACCS decides whether an output is permitted and consistent; it does not generate the underlying content.

3. Design
Two components over the output path:
	•	Compliance core. The gate is a possibly-non-terminating process observing candidate actions. It is modelled coinductively — as a greatest fixed point — so guarantees concern all infinite behaviours, not termination. The policy is a declarative specification; its clauses are the safety properties the relation must preserve.
	•	Consistency scorer. A functional mapping an output to a calibrated factual-consistency score with an attached provenance chain. It is designed so the score can be consumed as a penalty/objective term by a separate optimizer, without ACCS knowing that optimizer's goal.

4. Research Track (deliver as one document)
R1 — Soundness of gate decisions. Model the gating loop coinductively; treat the declarative policy clauses as properties of the greatest-fixed-point relation. Deliverable: a proof, by coinduction, that the agent never violates the policy on any infinite trace (safety), plus a liveness statement that a violation, once present, is detected within bounded steps. Acceptance: safety invariant proven; any up-to technique shown sound; liveness bound stated.
R2 — A calibrated factual-consistency score. Derive a scoring functional over outputs with provenance. Deliverable: proofs of the properties that make it usable as a meter — monotonicity (better-corroborated outputs do not score lower) and a calibration statement (the score tracks ground-truth consistency on a labelled set) — and a clean penalty-signal interface. Acceptance: monotonicity + calibration proven/demonstrated; penalty-signal interface specified.

5. Build Track — API Features
F1 — `policy.check`. Runs the compliance check on a candidate action/output and returns a permit/deny verdict with the policy clause responsible for any denial.
F2 — `consistency.score`. Scores an output, returning the calibrated consistency value, its provenance chain, and the penalty signal an external optimizer can consume.

6. Interface Contract
	•	Inputs: candidate actions/outputs to check or score; a declarative policy spec.
	•	Outputs: verdict records { permit: bool, clause } and score records { value, provenance, penalty }.
	•	Guarantee: policy.check and consistency.score sit on the output path so nothing is emitted without passing both.

7. Definition of Done
	•	One research document covering R1–R2 with proofs.
	•	F1–F2 implemented; the gate and scorer are wired onto the emission path.
	•	Contract conformance verified with a test fixture, including an injected policy violation.

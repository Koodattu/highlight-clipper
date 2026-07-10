# Use a bounded semantic evaluator

Deterministic generators will retrieve Candidate Moments broadly, while a local instruction model will inspect only their Context Envelopes and return validated, timestamp-evidenced Clip Proposals. We reject using the model to inspect the complete Source Recording or emit an opaque final score because bounded structured evaluation is cheaper, testable, and auditable, accepting a staged pipeline in exchange.

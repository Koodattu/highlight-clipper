# Generate candidate moments independently

Candidate discovery will use independent high-recall generators for each Observation family, beginning with transcript semantics and audio, then merge and deduplicate their Candidate Moments before semantic evaluation. Later chat, historical, visual, or game-specific generators follow the same contract. We reject a single combined "excitement score" because it would let conspicuous signals bury quiet semantic moments and obscure why each moment was retrieved, accepting more intermediate candidates in exchange for recall and category diversity.

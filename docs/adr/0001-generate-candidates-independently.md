# Generate candidate moments independently

Candidate discovery will use independent high-recall generators for transcript semantics and audio Observations, then merge and deduplicate their Candidate Moments before semantic evaluation. We reject a single combined "excitement score" because it would let conspicuous audio bury quiet semantic moments and obscure why each moment was retrieved, accepting more intermediate candidates in exchange for recall and category diversity.

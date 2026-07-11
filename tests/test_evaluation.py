from __future__ import annotations

import unittest

from highlight_clipper.workflows.evaluate import ProposalMoment, ReferenceMoment, match_proposals


class MatchingPolicyTests(unittest.TestCase):
    def test_maximum_cardinality_uses_lowest_total_rank(self) -> None:
        reference = ReferenceMoment("r1", "set1", "story", "en", 0, 10_000_000, 5_000_000)
        proposals = [
            ProposalMoment("p2", 2, "story", 0, 10_000_000, 5_000_000),
            ProposalMoment("p1", 1, "story", 0, 10_000_000, 5_000_000),
        ]
        matches = match_proposals(proposals, [reference], source_end_us=20_000_000)
        self.assertEqual([(pair.proposal.id, pair.reference.id) for pair in matches], [("p1", "r1")])

    def test_equal_rank_set_chooses_the_assignment_with_more_overlap(self) -> None:
        references = [
            ReferenceMoment("early", "set1", "story", "en", 0, 12_000_000, 5_000_000),
            ReferenceMoment("late", "set2", "story", "en", 8_000_000, 20_000_000, 15_000_000),
        ]
        proposals = [
            ProposalMoment("wide-early", 1, "story", 0, 11_000_000, 9_000_000),
            ProposalMoment("wide-late", 2, "story", 9_000_000, 20_000_000, 10_000_000),
        ]
        matches = match_proposals(proposals, references, source_end_us=20_000_000)
        self.assertEqual(
            {(pair.proposal.id, pair.reference.id) for pair in matches},
            {("wide-early", "early"), ("wide-late", "late")},
        )

    def test_residual_equal_cost_paths_terminate_without_a_predecessor_cycle(self) -> None:
        proposals = [
            ProposalMoment("p0", 1, "story", 45, 67, 66),
            ProposalMoment("p1", 2, "story", 22, 70, 55),
            ProposalMoment("p2", 3, "story", 70, 100, 91),
        ]
        references = [
            ReferenceMoment("r0", "s0", "story", "en", 61, 68, 61),
            ReferenceMoment("r1", "s1", "story", "en", 84, 98, 84),
            ReferenceMoment("r2", "s2", "story", "en", 53, 94, 53),
            ReferenceMoment("r3", "s3", "story", "en", 45, 95, 45),
            ReferenceMoment("r4", "s4", "story", "en", 11, 52, 11),
        ]

        matches = match_proposals(proposals, references, source_end_us=100)

        self.assertEqual(len(matches), 3)
        self.assertEqual(len({pair.proposal.id for pair in matches}), 3)
        self.assertEqual(len({pair.reference.id for pair in matches}), 3)


if __name__ == "__main__":
    unittest.main()

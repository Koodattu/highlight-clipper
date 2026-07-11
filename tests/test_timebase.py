from __future__ import annotations

import unittest
from decimal import Decimal
from fractions import Fraction

from highlight_clipper.domain import ProposalStructure
from highlight_clipper.timebase import (
    SourceInterval,
    seconds_to_us,
    timestamp_to_source_us,
    us_to_seconds,
)


class TimebaseTests(unittest.TestCase):
    def test_decimal_round_trip_uses_integer_microseconds(self) -> None:
        self.assertEqual(seconds_to_us("1.2345675"), 1_234_568)
        self.assertEqual(us_to_seconds(1_234_568), Decimal("1.234568"))

    def test_timestamp_is_rebased_once(self) -> None:
        self.assertEqual(timestamp_to_source_us(150, Fraction(1, 100), "1.25"), 250_000)

    def test_half_open_interval(self) -> None:
        interval = SourceInterval(10, 20)
        self.assertTrue(interval.contains_point(10))
        self.assertFalse(interval.contains_point(20))
        self.assertFalse(interval.overlaps(SourceInterval(20, 30)))

    def test_structure_allows_hook_before_setup_but_not_after_event(self) -> None:
        interval = SourceInterval(0, 100)
        ProposalStructure(event_us=50, hook_us=10, setup_start_us=20, exit_us=100).validate(interval)
        with self.assertRaises(ValueError):
            ProposalStructure(event_us=50, hook_us=51).validate(interval)


if __name__ == "__main__":
    unittest.main()

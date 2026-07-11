from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

MICROSECONDS_PER_SECOND = 1_000_000


def seconds_to_us(value: Decimal | float | int | str) -> int:
    seconds = Decimal(str(value))
    if not seconds.is_finite() or seconds < 0:
        raise ValueError("Source Time must be a finite non-negative value")
    return int((seconds * MICROSECONDS_PER_SECOND).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def us_to_seconds(value: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Source Time must be a non-negative integer number of microseconds")
    return Decimal(value) / MICROSECONDS_PER_SECOND


def timestamp_to_source_us(
    timestamp: int,
    time_base: Fraction,
    source_origin_seconds: Decimal | float | int | str,
) -> int:
    absolute = Decimal(timestamp * time_base.numerator) / Decimal(time_base.denominator)
    relative = absolute - Decimal(str(source_origin_seconds))
    return seconds_to_us(relative)


@dataclass(frozen=True, slots=True)
class SourceInterval:
    start_us: int
    end_us: int

    def __post_init__(self) -> None:
        if isinstance(self.start_us, bool) or isinstance(self.end_us, bool):
            raise TypeError("Source Time values must be integers")
        if not isinstance(self.start_us, int) or not isinstance(self.end_us, int):
            raise TypeError("Source Time values must be integers")
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise ValueError("Source intervals must satisfy 0 <= start < end")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def validate_within(self, source_end_us: int) -> SourceInterval:
        if self.end_us > source_end_us:
            raise ValueError("Source interval exceeds the Source Recording")
        return self

    def contains_point(self, point_us: int) -> bool:
        return self.start_us <= point_us < self.end_us

    def overlaps(self, other: SourceInterval) -> bool:
        return self.start_us < other.end_us and other.start_us < self.end_us

    def intersection_us(self, other: SourceInterval) -> int:
        return max(0, min(self.end_us, other.end_us) - max(self.start_us, other.start_us))

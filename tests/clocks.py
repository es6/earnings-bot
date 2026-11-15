"""Clock fakes built on the burst replay clock, kept out of fakes.py to avoid an import cycle."""

from datetime import datetime, timedelta

from tests.burst_replay import LocalClock


class ShiftedClock(LocalClock):
    """This PC's clock moved forward by `shift`, so a test can skip to the end of a window."""

    shift = timedelta()

    def now(self) -> datetime:
        return super().now() + self.shift

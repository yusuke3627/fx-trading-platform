"""broker の壁時計ラベルと実 UTC の対応。"""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def broker_label_to_known(label: datetime, server_ahead_of_ny: timedelta) -> datetime:
    """A broker wall-clock label -> the real UTC instant it names.

    The server keeps New York close at its own midnight: its wall time is New
    York wall time plus a fixed anchor (7h) YEAR-ROUND, which lands at UTC+3
    during US DST and UTC+2 outside it. Subtracting the anchor and localizing
    in America/New_York therefore follows the DST switches without a season
    table.

    The DST transition hours themselves never name a tradable instant under
    this anchor: New York switches at 02:00 Sunday, inside the FX weekend
    close (Friday 17:00 - Sunday 17:00 New York). A label inside the repeated
    or skipped hour therefore contradicts the anchor assumption, and folding
    it onto either occurrence could hand the replay a future price an hour
    early — so it is refused instead of guessed.
    """
    naive_ny = label.astimezone(UTC).replace(tzinfo=None) - server_ahead_of_ny
    first = naive_ny.replace(tzinfo=NEW_YORK)
    if first.utcoffset() != naive_ny.replace(tzinfo=NEW_YORK, fold=1).utcoffset():
        raise ValueError(
            f"broker label {label.isoformat()} falls in a New York DST "
            "transition hour, which the New York-close anchor places inside "
            "the weekend close; the dataset contradicts the configured anchor"
        )
    return first.astimezone(UTC)


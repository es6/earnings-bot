"""Load-time compatibility for old schedules and persisted automatic IR patterns."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from earnings_bot.browser_sources import IRSource


def schedule_fields(values: Any) -> Any:
    """Promote old trial.ir without overriding an explicit current-format IR source."""
    if (
        isinstance(values, dict)
        and isinstance(trial := values.get("trial"), dict)
        and "ir" in trial
    ):
        return {
            "ir": trial["ir"],
            **values,
            "trial": {key: value for key, value in trial.items() if key != "ir"},
        }
    return values


def restore_ir_headline(ir: "IRSource | None", *, listing: str | None, headline: str) -> None:
    """Repair only the exact zero-width pattern persisted by the original auto planner."""
    if (
        ir is not None
        and ir.verify_text
        and ir.listing == listing
        and ir.headline == headline.removesuffix(".*")
    ):
        ir.headline = headline

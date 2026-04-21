from src.economics.models import ECONOMIC_MODEL_VERSION


def _qualified(alias: str | None, column: str) -> str:
    return f"{alias}.{column}" if alias else column


def valid_paper_trade_filter(alias: str | None = None) -> str:
    return (
        f"{_qualified(alias, 'economic_model_version')} = '{ECONOMIC_MODEL_VERSION}' "
        f"AND {_qualified(alias, 'invalidated_at')} IS NULL"
    )


def valid_decision_filter(alias: str | None = None) -> str:
    return (
        f"{_qualified(alias, 'economic_model_version')} = '{ECONOMIC_MODEL_VERSION}' "
        f"AND {_qualified(alias, 'invalidated_at')} IS NULL"
    )


def valid_position_filter(alias: str | None = None) -> str:
    return (
        f"{_qualified(alias, 'economic_model_version')} = '{ECONOMIC_MODEL_VERSION}' "
        f"AND {_qualified(alias, 'invalidated_at')} IS NULL"
    )


def valid_profile_learning_filter(alias: str | None = None) -> str:
    return (
        f"{_qualified(alias, 'economic_model_version')} = '{ECONOMIC_MODEL_VERSION}' "
        f"AND {_qualified(alias, 'learning_invalidated_at')} IS NULL"
    )

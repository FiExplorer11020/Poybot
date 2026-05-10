"""Global runtime control: killswitch, execution mode."""

from src.control.killswitch import (
    KillswitchService,
    KillswitchState,
    get_killswitch,
    set_killswitch,
)

__all__ = [
    "KillswitchService",
    "KillswitchState",
    "get_killswitch",
    "set_killswitch",
]

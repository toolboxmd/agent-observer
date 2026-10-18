"""Child-process environment for machine-readable GitHub CLI output."""

import os


def structured_env() -> dict[str, str]:
    """Disable terminal color/TTY forcing in a copy of the parent environment."""
    env = os.environ.copy()
    # CLICOLOR_FORCE overrides NO_COLOR; GH_FORCE_TTY can enable color too.
    env.update(NO_COLOR="1", CLICOLOR="0", CLICOLOR_FORCE="0")
    env.pop("GH_FORCE_TTY", None)
    return env

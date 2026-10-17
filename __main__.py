"""Use ``python -m wifivouchers`` to launch the application."""

import sys

from .wifivouchers import StartupError, main


if __name__ == "__main__":
    try:
        main()
    except StartupError as exc:
        sys.exit(exc.exit_code)

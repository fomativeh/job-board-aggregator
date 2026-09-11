from __future__ import annotations

import logging
import sys

from .cli import main


def _entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        log = logging.getLogger(__name__)
        log.warning("Interrupted by user - exiting 130")
        return 130
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.error("Unhandled error: %s: %s", type(exc).__name__, exc)
        return 1


sys.exit(_entrypoint())

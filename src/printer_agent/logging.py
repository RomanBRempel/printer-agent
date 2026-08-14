from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s",
    )

"""structlog configuration: JSON to stdout in prod, pretty console for local runs."""

import logging
import sys

import structlog


def setup_logging(*, json: bool = True, level: int = logging.INFO) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: structlog.typing.Processor
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

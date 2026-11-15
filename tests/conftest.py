"""Shared fixtures."""
import logging

import pytest


@pytest.fixture
def keep_logging():
    """alembic's env.py calls logging.config.fileConfig, which disables every existing logger and replaces the root handlers. Put the
    logging state back afterwards so tests that run later (log-safety and scheduler tests) still see their loggers."""
    root = logging.getLogger()
    state = {name: (lg.disabled, lg.level, lg.propagate, list(lg.handlers)) for name, lg in logging.root.manager.loggerDict.items() if isinstance(lg, logging.Logger)}
    root_state = (root.level, list(root.handlers))
    yield
    for name, (disabled, level, propagate, handlers) in state.items():
        lg = logging.getLogger(name)
        lg.disabled, lg.level, lg.propagate, lg.handlers = disabled, level, propagate, handlers
    root.setLevel(root_state[0])
    root.handlers = root_state[1]

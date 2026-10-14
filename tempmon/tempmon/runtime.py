"""Wiring helpers shared by the CLI scripts and the dashboard."""
from __future__ import annotations

import logging

from .config import Config
from .notifier import LogNotifier, WebhookNotifier
from .processor import Processor
from .store import Store


def build(cfg: Config | None = None, extra_notifiers=()) -> Processor:
    cfg = cfg or Config()
    notifiers = [LogNotifier(), *extra_notifiers]
    if cfg.webhook_url:
        notifiers.append(WebhookNotifier(cfg.webhook_url))
    return Processor(cfg, Store(cfg.db_path), notifiers)


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

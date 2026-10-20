"""Dummy smoke agent — no-LLM scripted responder for a2a (pair) runs.

Each turn it acks (or replays) a reply and, after a configured number of
turns, writes the end-conversation marker. Used to validate the pair
lifecycle — relay, per-turn executor cycle, end detection — without
spending model tokens.
"""

from .config import DummyConfig
from .deployer import DummyDeployer

__all__ = ["DummyConfig", "DummyDeployer"]

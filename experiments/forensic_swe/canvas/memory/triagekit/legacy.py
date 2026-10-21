"""Deprecated: timeline v1. Use triagekit.events.Event."""

EVENTS = []


def emit(ts, host, user, action):
    EVENTS.append((ts, host, user, action))

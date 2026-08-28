"""Prime Agent minion bridge for Hermes."""

from __future__ import annotations


def register(ctx):
    from .tools import register_tools

    register_tools(ctx)


__all__ = ["register"]

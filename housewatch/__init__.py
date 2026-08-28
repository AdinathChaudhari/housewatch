"""housewatch — whole-house WAN usage tracker.

Polls the router's UPnP IGD service for cumulative WAN byte/packet counters,
reconstructs true usage across ui4 (2**32) counter wraps and router reboots,
and serves the result via a menubar app and a local Flask dashboard.

See housewatch/CLAUDE.md for the wrap-safety design and operational notes.
"""

__version__ = "0.1.0"

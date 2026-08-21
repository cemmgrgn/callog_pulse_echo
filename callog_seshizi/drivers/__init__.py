"""Drivers specific to callog_seshizi: Keysight DSOX3012T / InfiniiVision
oscilloscopes and their simulator. Registered into the shared
`callog_common.drivers.REGISTRY` from `callog_seshizi/__init__.py` — this
subpackage doesn't register itself, so importing it alone (without the
parent package) won't make the drivers available via `drivers.create()`.
"""

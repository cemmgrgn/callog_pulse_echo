"""CalLog Pulse-Echo — ultrasonic pulse-echo velocity/thickness measurement.

Built on `callog_common` (auth, certificates, audit, database, backup — the
lab-wide infrastructure shared with `callog_defib`). This package adds only
what's specific to ultrasonic measurement: the DSP pipeline, the ML
thickness model, the DSOX3012T/InfiniiVision drivers, and the velocity page.

Importing this package registers its test mode into the shared
`callog_common.testmodes` registry and its drivers into
`callog_common.drivers` — anything that lists `testmodes.MODES` or
`drivers.REGISTRY` needs this import to have happened first (`run.py`
does it on startup).
"""

from callog_common import drivers as _drivers

from . import pulse_echo_modes  # noqa: F401  (registers the sound_velocity mode)
from .drivers.keysight_dsox3012t import KeysightDSOX3012T
from .drivers.simulated_ultrasonic import SimulatedUltrasonic

_drivers.register_driver("dsox3012t", KeysightDSOX3012T)
_drivers.register_driver("simulated_ultrasonic", SimulatedUltrasonic, simulated=True)

__version__ = "0.1.0"
__author__ = "Cem Girgin"

"""picoscope-4824a — driver, CLI, and web control surface for a PicoScope 4824A.

The public surface is deliberately small::

    from picoscope_4824a import PicoScope4824A, Channel, Range, Coupling

    with PicoScope4824A() as scope:
        scope.set_channel(Channel.A, True, voltage_range=Range.R_5V)
        capture = scope.capture_block(n_samples=10_000, sample_rate_hz=1e6)

Everything a user can do from the GUI is also a method on
:class:`~picoscope_4824a.controller.ScopeController`, and every one of those is
exposed as a CLI subcommand and an HTTP route — see
:mod:`picoscope_4824a.registry` for the single command table all three render.
"""

from .ps4000a import (Channel, Coupling, PicoError, PicoSDKNotFound, RatioMode,
                      Range, SweepType, ThresholdDirection, TimeUnits, WaveType)
from .scope import (Capture, ChannelConfig, PicoScope4824A, TriggerConfig,
                    enumerate_units)

__version__ = "0.1.0"

__all__ = [
    "PicoScope4824A", "enumerate_units",
    "Capture", "ChannelConfig", "TriggerConfig",
    "Channel", "Coupling", "Range", "RatioMode", "TimeUnits",
    "ThresholdDirection", "WaveType", "SweepType",
    "PicoError", "PicoSDKNotFound",
    "__version__",
]

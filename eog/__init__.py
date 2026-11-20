"""EOG eye-movement processing: drift-removal methods, direction detection,
metrics, serial/CSV I/O and offline replay.

Run ``python -m eog --help`` for the command-line interface.
"""

from .detector import Calibrator, DirectionDetector
from .methods import COMPARISON_ORDER, METHODS, get_method
from .pipeline import ChannelPipeline

__all__ = ["Calibrator", "DirectionDetector", "ChannelPipeline", "METHODS",
           "COMPARISON_ORDER", "get_method"]
__version__ = "1.0.0"

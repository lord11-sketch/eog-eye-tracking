"""ADC-count <-> physical-unit conversion.

All thresholds and metrics in this project are computed in **raw ADC counts**.
The original scripts labelled some of them "uV"; they were never converted.

Two conversions are available:

* counts -> volts *at the ADC pin* needs only the ADC resolution and reference
  voltage (both known from the firmware: 14-bit, default 5 V reference on the
  Uno R4 - nominal, measure the board for an accurate figure);
* counts -> microvolts *at the electrodes* additionally needs the total gain
  of the analogue front end.  That gain is not recorded anywhere in the
  project sources, so it has no default: it must be supplied explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AdcConfig:
    bits: int = 14
    vref: float = 5.0
    """ADC reference voltage in volts (nominal; USB-powered boards vary)."""
    gain: Optional[float] = None
    """Total front-end voltage gain (V/V).  ``None`` = unknown."""

    @property
    def full_scale(self) -> int:
        return (1 << self.bits) - 1

    @property
    def volts_per_count(self) -> float:
        return self.vref / (1 << self.bits)

    def counts_to_mv_at_adc(self, counts: float) -> float:
        """Millivolts at the ADC input for a *difference* of ``counts``."""
        return counts * self.volts_per_count * 1e3

    def counts_to_uv_at_electrode(self, counts: float) -> Optional[float]:
        """Input-referred microvolts, or ``None`` when the gain is unknown."""
        if not self.gain:
            return None
        return counts * self.volts_per_count / self.gain * 1e6

    def describe(self) -> dict:
        return {
            "adc_bits": self.bits,
            "adc_vref_v_nominal": self.vref,
            "mv_at_adc_per_count": self.counts_to_mv_at_adc(1.0),
            "frontend_gain": self.gain,
            "uv_at_electrode_per_count": self.counts_to_uv_at_electrode(1.0),
        }

"""Typed, Home Assistant-independent Ocea API models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ConsumptionRecord:
    """One daily consumption observation."""

    date: datetime
    value: float


@dataclass(frozen=True, slots=True)
class MeterData:
    """A physical Ocea meter and its daily history."""

    meter_id: str
    serial_number: str
    meter_type: str
    current_value: float
    history: list[ConsumptionRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DwellingData:
    """Physical dwelling information."""

    dwelling_id: str
    address: str | None
    meters: list[MeterData] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OccupancyData:
    """Resident occupancy contract and its consumption data."""

    occupancy_id: str
    is_active: bool
    dwelling: DwellingData
    cold_water_total: float
    hot_water_total: float
    heating_total: float | None
    has_leak: bool
    leak_estimate: float

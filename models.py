"""
Pydantic models for the HVAC Dashboard API.
Pure data-shape definitions — no imports from state.py or worker.py,
so this module can be imported from anywhere without circular-import risk.
"""

from typing import Optional, List
from pydantic import BaseModel


class DeviceConfig(BaseModel):
    host: str
    name: str
    btu: int = 24000
    seer: int = 20
    max_temp: Optional[float] = None
    beeper: str = "OFF"
    watchdog_minutes: int = 5
    lock_temp: bool = False
    locked_target_temp: Optional[float] = None
    has_ir_emitter: bool = False


class CommandPayload(BaseModel):
    params: dict


class MaintenanceConfig(BaseModel):
    id: Optional[str] = None
    name: str
    device_host: Optional[str] = None
    trigger_type: str = "days"          # "days" or "runtime_hours"
    interval_days: Optional[int] = None
    interval_hours: Optional[float] = None
    last_done_at: Optional[str] = None
    notes: Optional[str] = None


class ScheduleConfig(BaseModel):
    id: Optional[str] = None
    device_host: str
    device_name: str
    time: str
    end_time: Optional[str] = None
    days: List[int]
    power: Optional[str] = None
    mode: Optional[str] = None
    temp: Optional[float] = None
    enabled: bool = True

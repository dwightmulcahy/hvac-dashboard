"""
Pydantic models for the HVAC Dashboard API.
Pure data-shape definitions — no imports from state.py or worker.py,
so this module can be imported from anywhere without circular-import risk.
"""


from pydantic import BaseModel


class DeviceConfig(BaseModel):
    host: str
    name: str
    btu: int = 24000
    seer: int = 20
    max_temp: float | None = None
    beeper: str = "OFF"
    watchdog_minutes: int = 5
    lock_temp: bool = False
    locked_target_temp: float | None = None
    has_ir_emitter: bool = False


class CommandPayload(BaseModel):
    params: dict


class MaintenanceConfig(BaseModel):
    id: str | None = None
    name: str
    device_host: str | None = None
    trigger_type: str = "days"          # "days" or "runtime_hours"
    interval_days: int | None = None
    interval_hours: float | None = None
    last_done_at: str | None = None
    notes: str | None = None


class ScheduleConfig(BaseModel):
    id: str | None = None
    device_host: str
    device_name: str
    time: str
    end_time: str | None = None
    days: list[int]
    power: str | None = None
    mode: str | None = None
    temp: float | None = None
    enabled: bool = True

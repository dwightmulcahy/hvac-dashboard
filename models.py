"""
Pydantic models for the HVAC Dashboard API.
Pure data-shape definitions — no imports from state.py or worker.py,
so this module can be imported from anywhere without circular-import risk.
"""


from pydantic import BaseModel, Field, model_validator


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
    keep_mode: bool = False


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


class SleepCurve(BaseModel):
    """Overnight setpoint curve for a COOL/HEAT schedule with an end time:
    drift `rise`°C away from the schedule temp (warmer in COOL, cooler in
    HEAT) over `rise_hours`, hold, then return to `wake_temp` (default: the
    schedule temp) over the last `wake_hours` before end_time."""
    rise: float = Field(2.0, ge=0.5, le=3.0)
    rise_hours: float = Field(2.0, ge=0, le=4)
    wake_hours: float = Field(2.0, ge=0, le=3)
    wake_temp: float | None = Field(None, ge=17, le=30)
    step: float = 0.5

    @model_validator(mode="after")
    def _step_valid(self):
        if self.step not in (0.5, 1.0):
            raise ValueError("step must be 0.5 or 1.0")
        return self


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
    eco: str | None = None          # "on" / "off" — unit ECO preset; None = leave as is
    sleep: SleepCurve | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _sleep_requirements(self):
        if self.sleep is None:
            return self
        if not self.end_time:
            raise ValueError("sleep curve needs an end time")
        if self.mode not in ("COOL", "HEAT") or self.temp is None or self.power == "off":
            raise ValueError("sleep curve needs mode COOL or HEAT and a temperature")
        if self.eco == "on":
            raise ValueError("sleep curve can't be combined with ECO on (ECO forces its own setpoint)")
        return self

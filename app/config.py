"""Clinic settings, read from environment variables with sensible defaults."""

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

# Weekday (Monday=0 ... Sunday=6) -> list of (open, close) intervals.
DEFAULT_WORKING_HOURS: dict[int, list[tuple[time, time]]] = {
    0: [(time(9), time(13)), (time(14), time(18))],
    1: [(time(9), time(13)), (time(14), time(18))],
    2: [(time(9), time(13)), (time(14), time(18))],
    3: [(time(9), time(13)), (time(14), time(18))],
    4: [(time(9), time(12)), (time(15), time(18))],  # Friday: long midday break
    5: [(time(9), time(13))],
    6: [],  # Sunday: closed
}


@dataclass(frozen=True)
class Settings:
    clinic_name: str = os.getenv("CLINIC_NAME", "عيادة الدكتور")
    doctor_name: str = os.getenv("DOCTOR_NAME", "الدكتور أحمد")
    timezone: ZoneInfo = field(
        default_factory=lambda: ZoneInfo(os.getenv("CLINIC_TIMEZONE", "Africa/Casablanca"))
    )
    slot_minutes: int = int(os.getenv("SLOT_MINUTES", "30"))
    # How far ahead patients may book.
    booking_horizon_days: int = int(os.getenv("BOOKING_HORIZON_DAYS", "60"))
    working_hours: dict[int, list[tuple[time, time]]] = field(
        default_factory=lambda: DEFAULT_WORKING_HOURS
    )
    database_path: str = os.getenv("DATABASE_PATH", "clinic.db")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    # Phone conversations need low latency; raise for harder conversations.
    claude_effort: str = os.getenv("CLAUDE_EFFORT", "low")
    # Server-side refusal fallback (beta). Set to "0" to disable.
    claude_fallbacks: bool = os.getenv("CLAUDE_FALLBACKS", "1") == "1"


settings = Settings()

"""Booking rules shared by every channel (web calendar and phone agent)."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Settings
from .db import Database

FMT = "%Y-%m-%dT%H:%M"


class BookingError(Exception):
    """A booking request that breaks a rule; the message is safe to show patients."""


class SlotUnavailable(BookingError):
    pass


@dataclass
class Appointment:
    id: int
    patient_name: str
    phone: str
    start: str
    end: str
    reason: str | None
    channel: str
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Appointment":
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})


def normalize_phone(phone: str) -> str:
    """Keep digits and a leading '+', so '06 12-34' and '061234' match."""
    phone = phone.strip()
    digits = re.sub(r"\D", "", phone)
    if not digits:
        raise BookingError("رقم الهاتف غير صالح")
    return ("+" if phone.startswith("+") else "") + digits


class Scheduler:
    def __init__(self, db: Database, settings: Settings, now=None):
        self.db = db
        self.settings = settings
        # Injectable clock for tests; returns naive clinic-local time.
        self._now = now or (lambda: datetime.now(settings.timezone).replace(tzinfo=None))

    def now(self) -> datetime:
        return self._now()

    # ---- availability -------------------------------------------------

    def _all_slots(self, day: date) -> list[datetime]:
        step = timedelta(minutes=self.settings.slot_minutes)
        slots = []
        for open_t, close_t in self.settings.working_hours.get(day.weekday(), []):
            t = datetime.combine(day, open_t)
            end = datetime.combine(day, close_t)
            while t + step <= end:
                slots.append(t)
                t += step
        return slots

    def _booked_starts(self, day: date) -> set[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT start FROM appointments WHERE status = 'booked' AND start LIKE ?",
                (day.isoformat() + "T%",),
            ).fetchall()
        return {r["start"] for r in rows}

    def _in_horizon(self, day: date) -> bool:
        today = self.now().date()
        return today <= day <= today + timedelta(days=self.settings.booking_horizon_days)

    def available_slots(self, day: date) -> list[datetime]:
        if not self._in_horizon(day):
            return []
        now = self.now()
        booked = self._booked_starts(day)
        return [s for s in self._all_slots(day) if s > now and s.strftime(FMT) not in booked]

    def next_available_days(self, start_day: date, max_days: int = 5) -> list[tuple[date, list[datetime]]]:
        """The first `max_days` open days on or after `start_day` with free slots."""
        out = []
        day = max(start_day, self.now().date())
        while len(out) < max_days and self._in_horizon(day):
            slots = self.available_slots(day)
            if slots:
                out.append((day, slots))
            day += timedelta(days=1)
        return out

    # ---- bookings -----------------------------------------------------

    def book(self, patient_name: str, phone: str, start: datetime, reason: str | None, channel: str) -> Appointment:
        patient_name = patient_name.strip()
        if not patient_name:
            raise BookingError("اسم المريض مطلوب")
        phone = normalize_phone(phone)
        start = start.replace(second=0, microsecond=0, tzinfo=None)
        if start not in self._all_slots(start.date()):
            raise BookingError("هذا الموعد خارج أوقات عمل العيادة")
        if start <= self.now() or not self._in_horizon(start.date()):
            raise BookingError("لا يمكن الحجز في هذا التاريخ")
        end = start + timedelta(minutes=self.settings.slot_minutes)
        try:
            with self.db.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO appointments (patient_name, phone, start, end, reason, channel)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (patient_name, phone, start.strftime(FMT), end.strftime(FMT), reason, channel),
                )
                row = conn.execute("SELECT * FROM appointments WHERE id = ?", (cur.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            raise SlotUnavailable("هذا الموعد محجوز بالفعل")
        return Appointment.from_row(row)

    def upcoming_for_phone(self, phone: str) -> list[Appointment]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE phone = ? AND status = 'booked' AND start >= ?"
                " ORDER BY start",
                (normalize_phone(phone), self.now().strftime(FMT)),
            ).fetchall()
        return [Appointment.from_row(r) for r in rows]

    def cancel(self, appointment_id: int, phone: str) -> Appointment:
        """Cancel an appointment; the phone number must match the booking's."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id = ? AND phone = ? AND status = 'booked'",
                (appointment_id, normalize_phone(phone)),
            ).fetchone()
            if row is None:
                raise BookingError("لم يتم العثور على هذا الموعد")
            conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,))
        appt = Appointment.from_row(row)
        appt.status = "cancelled"
        return appt

    def list_for_day(self, day: date) -> list[Appointment]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = 'booked' AND start LIKE ? ORDER BY start",
                (day.isoformat() + "T%",),
            ).fetchall()
        return [Appointment.from_row(r) for r in rows]

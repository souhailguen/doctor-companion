from datetime import date, datetime

import pytest

from app.scheduling import BookingError, SlotUnavailable, normalize_phone


def test_slots_skip_past_times_and_breaks(scheduler):
    slots = [s.strftime("%H:%M") for s in scheduler.available_slots(date(2026, 10, 12))]
    assert slots[0] == "10:30"  # 10:00 already started at 10:05
    assert "13:00" not in slots and "13:30" not in slots  # lunch break
    assert slots[-1] == "17:30"


def test_closed_on_sunday_and_outside_horizon(scheduler):
    assert scheduler.available_slots(date(2026, 10, 18)) == []
    assert scheduler.available_slots(date(2027, 3, 1)) == []
    assert scheduler.available_slots(date(2026, 10, 11)) == []


def test_booking_removes_slot_and_blocks_double_booking(scheduler):
    start = datetime(2026, 10, 13, 9, 0)
    appt = scheduler.book("Fatima Zahra", "06 12 34 56 78", start, "checkup", "web")
    assert appt.phone == "0612345678"
    assert start not in scheduler.available_slots(start.date())
    with pytest.raises(SlotUnavailable):
        scheduler.book("Someone Else", "0700000000", start, None, "phone")


def test_rejects_invalid_slots(scheduler):
    with pytest.raises(BookingError):
        scheduler.book("A B", "0612345678", datetime(2026, 10, 13, 9, 15), None, "web")  # off-grid
    with pytest.raises(BookingError):
        scheduler.book("A B", "0612345678", datetime(2026, 10, 13, 13, 0), None, "web")  # lunch
    with pytest.raises(BookingError):
        scheduler.book("A B", "0612345678", datetime(2026, 10, 12, 9, 0), None, "web")  # past


def test_cancel_requires_matching_phone_and_frees_slot(scheduler):
    start = datetime(2026, 10, 14, 11, 0)
    appt = scheduler.book("Youssef", "+212612345678", start, None, "phone")
    with pytest.raises(BookingError):
        scheduler.cancel(appt.id, "0700000000")
    assert [a.id for a in scheduler.upcoming_for_phone("+212 612-345-678")] == [appt.id]
    scheduler.cancel(appt.id, "+212612345678")
    assert scheduler.upcoming_for_phone("+212612345678") == []
    # Slot can be booked again after cancellation.
    scheduler.book("Other", "0611111111", start, None, "web")


def test_next_available_days_skips_full_and_closed_days(scheduler):
    days = scheduler.next_available_days(date(2026, 10, 17), max_days=2)
    assert [d.isoformat() for d, _ in days] == ["2026-10-17", "2026-10-19"]  # Sunday skipped


def test_normalize_phone():
    assert normalize_phone(" +212 6-12 ") == "+212612"
    with pytest.raises(BookingError):
        normalize_phone("abc")

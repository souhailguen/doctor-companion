"""FastAPI app: web booking API, voice gateway API, and static pages."""

import logging
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import SecretaryAgent
from .config import Settings, settings as default_settings
from .db import Database
from .scheduling import BookingError, Scheduler, SlotUnavailable

STATIC = Path(__file__).parent / "static"


class BookingIn(BaseModel):
    patient_name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=6, max_length=20)
    start: datetime
    reason: str | None = Field(default=None, max_length=300)


class CancelIn(BaseModel):
    phone: str


class CallStartIn(BaseModel):
    caller_phone: str | None = None


class CallTurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def create_app(settings: Settings = default_settings, agent: SecretaryAgent | None = None,
               scheduler: Scheduler | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    scheduler = scheduler or Scheduler(Database(settings.database_path), settings)
    app = FastAPI(title="Doctor Companion")
    app.state.scheduler = scheduler
    app.state.agent = agent  # created lazily so the web API runs without an API key

    def get_agent() -> SecretaryAgent:
        if app.state.agent is None:
            app.state.agent = SecretaryAgent(scheduler, settings)
        return app.state.agent

    # ---- web channel ----------------------------------------------------

    @app.get("/api/config")
    def config():
        return {
            "clinic_name": settings.clinic_name,
            "doctor_name": settings.doctor_name,
            "slot_minutes": settings.slot_minutes,
            "today": scheduler.now().date().isoformat(),
            "booking_horizon_days": settings.booking_horizon_days,
        }

    @app.get("/api/availability")
    def availability(start: date, days: int = Query(7, ge=1, le=31)):
        """Free slots per day, for rendering the calendar."""
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            out.append({"date": d.isoformat(), "slots": [s.strftime("%H:%M") for s in scheduler.available_slots(d)]})
        return out

    @app.post("/api/appointments", status_code=201)
    def book(body: BookingIn):
        try:
            appt = scheduler.book(body.patient_name, body.phone, body.start, body.reason, channel="web")
        except SlotUnavailable as e:
            raise HTTPException(409, str(e))
        except BookingError as e:
            raise HTTPException(422, str(e))
        return {"id": appt.id, "start": appt.start, "end": appt.end}

    @app.get("/api/appointments")
    def lookup(phone: str):
        # Public lookup only returns times, not names or reasons.
        try:
            appts = scheduler.upcoming_for_phone(phone)
        except BookingError as e:
            raise HTTPException(422, str(e))
        return [{"id": a.id, "start": a.start, "end": a.end} for a in appts]

    @app.post("/api/appointments/{appointment_id}/cancel")
    def cancel(appointment_id: int, body: CancelIn):
        try:
            appt = scheduler.cancel(appointment_id, body.phone)
        except BookingError as e:
            raise HTTPException(404, str(e))
        return {"id": appt.id, "status": appt.status}

    # ---- clinic staff ---------------------------------------------------

    @app.get("/api/admin/appointments")
    def admin_day(day: date, x_admin_token: str | None = Header(default=None)):
        token = os.getenv("ADMIN_TOKEN")
        if not token or not x_admin_token or not secrets.compare_digest(token, x_admin_token):
            raise HTTPException(401, "unauthorized")
        return [a.__dict__ for a in scheduler.list_for_day(day)]

    # ---- phone channel --------------------------------------------------
    # Provider-agnostic voice gateway: the telephony adapter does speech-to-text,
    # posts each caller utterance here, and speaks back `reply`.

    @app.post("/voice/calls")
    def start_call(body: CallStartIn):
        conv = get_agent().start(channel="phone", caller_phone=body.caller_phone)
        return {"call_id": conv.id, "reply": conv.greeting, "end_call": False}

    @app.post("/voice/calls/{call_id}/turn")
    async def call_turn(call_id: str, body: CallTurnIn):
        agent = get_agent()
        conv = agent.get(call_id)
        if conv is None or conv.ended:
            raise HTTPException(404, "call not found")
        result = await agent.reply(conv, body.text)
        return {"call_id": conv.id, "reply": result.text, "end_call": result.end_call}

    # ---- pages ------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/call", include_in_schema=False)
    def call_page():
        return FileResponse(STATIC / "call.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()

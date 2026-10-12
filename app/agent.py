"""The AI secretary: a Claude tool-use loop over the clinic scheduler.

The same agent serves every conversational channel. The voice gateway feeds it
transcribed caller speech and speaks its replies; the transport (Twilio,
Vonage, a SIP trunk, the browser simulator) only has to move text in and out.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import anthropic

from .config import Settings
from .scheduling import FMT, BookingError, Scheduler

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
CONVERSATION_TTL_SECONDS = 2 * 3600

SYSTEM_PROMPT = """You are the secretary of {clinic_name}, the medical practice of {doctor_name}. \
You answer patients by phone and by chat, and your job is to book, look up and cancel \
appointments.

Language: always reply in Arabic. Mirror the patient's register: if they speak a dialect \
(Moroccan Darija, Egyptian, Levantine, Gulf...), answer in that dialect; otherwise use \
simple Modern Standard Arabic. Keep the tone warm, polite and brief.

How to book:
- Ask for what is missing, one question at a time: preferred day (and time of day), the \
patient's full name, and a short reason for the visit. On the phone the caller's number is \
given in the conversation context; confirm it is the right number for the booking rather \
than asking them to dictate it, unless it is missing.
- Always call check_availability before proposing times. Offer at most three times at once. \
Never invent availability.
- Before calling book_appointment, read the details back (name, day, date, time) and get an \
explicit yes. After booking, confirm the day, date and time once more.
- To cancel or move an appointment, look it up with find_appointments using the patient's \
phone number. Moving = book the new slot first, then cancel the old one.

Limits:
- You are not a doctor. Do not give medical advice, diagnoses or medication guidance.
- If the patient describes an emergency (chest pain, difficulty breathing, heavy bleeding, \
loss of consciousness, stroke signs, suicidal thoughts...), tell them immediately to call \
the emergency services (15 in Morocco, 997 in Saudi Arabia, 123 in Egypt, 112 elsewhere) \
or go to the nearest emergency room, and do not continue booking.
- For anything other than appointments (results, prescriptions, prices you do not know), \
say the clinic team will get back to them.

Phone channel style: your words are converted to speech. Use short spoken sentences, no \
lists, no emojis, no markdown, no symbols. Say dates and times the way people say them \
aloud (for example "يوم الثلاثاء ثلاثة عشر أكتوبر على الساعة العاشرة صباحاً"). When the \
caller is done and has said goodbye, say a short goodbye and call end_call."""

TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "List free appointment times on a given date. If the date has no free time, "
            "also returns the next days that do. Dates are clinic local time."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Date to check, YYYY-MM-DD."},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Book one appointment. Only call after the patient explicitly confirmed the "
            "name, date and time you read back to them."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_name": {"type": "string", "description": "Patient's full name."},
                "phone": {"type": "string", "description": "Patient's phone number."},
                "start": {"type": "string", "description": "Slot start, YYYY-MM-DDTHH:MM, from check_availability."},
                "reason": {"type": "string", "description": "Short reason for the visit, or empty string."},
            },
            "required": ["patient_name", "phone", "start", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_appointments",
        "description": "List the upcoming appointments booked under a phone number.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_appointment",
        "description": (
            "Cancel an appointment found with find_appointments. Only call after the "
            "patient confirmed which appointment to cancel."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "integer"},
                "phone": {"type": "string", "description": "Phone number the appointment is booked under."},
            },
            "required": ["appointment_id", "phone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "end_call",
        "description": "Hang up the phone call. Use only after saying goodbye.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
]

FALLBACK_REPLY = "عذراً، حدث خطأ تقني. سيتصل بك فريق العيادة في أقرب وقت."


@dataclass
class Conversation:
    channel: str  # "phone" | "web"
    caller_phone: str | None = None
    greeting: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list = field(default_factory=list)
    ended: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class AgentReply:
    text: str
    end_call: bool = False


def _slot_view(dt: datetime) -> dict:
    return {"start": dt.strftime(FMT), "weekday": dt.strftime("%A"), "time": dt.strftime("%H:%M")}


class SecretaryAgent:
    def __init__(self, scheduler: Scheduler, settings: Settings, client: anthropic.AsyncAnthropic | None = None):
        self.scheduler = scheduler
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic()
        # The system prompt never changes between requests, so it caches well.
        self.system = SYSTEM_PROMPT.format(clinic_name=settings.clinic_name, doctor_name=settings.doctor_name)
        self.conversations: dict[str, Conversation] = {}

    # ---- conversation lifecycle ---------------------------------------

    def greeting(self) -> str:
        return f"{self.settings.clinic_name}، السلام عليكم. كيف يمكنني مساعدتك؟"

    def start(self, channel: str, caller_phone: str | None = None) -> Conversation:
        # In-memory store for the MVP: drop conversations past their TTL.
        cutoff = time.monotonic() - CONVERSATION_TTL_SECONDS
        for cid in [c.id for c in self.conversations.values() if c.created_at < cutoff]:
            del self.conversations[cid]
        conv = Conversation(channel=channel, caller_phone=caller_phone, greeting=self.greeting())
        self.conversations[conv.id] = conv
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    def _context_block(self, conv: Conversation) -> str:
        now = self.scheduler.now()
        days = [now.date() + timedelta(days=i) for i in range(14)]
        calendar = ", ".join(f"{d.strftime('%a')} {d.isoformat()}" for d in days)
        return (
            "<conversation_context>\n"
            f"channel: {conv.channel}\n"
            f"caller phone number: {conv.caller_phone or 'unknown'}\n"
            f"now (clinic local time): {now.strftime('%A %Y-%m-%d %H:%M')}\n"
            f"next 14 days: {calendar}\n"
            f"you already greeted the patient with: {conv.greeting}\n"
            "</conversation_context>"
        )

    async def reply(self, conv: Conversation, user_text: str) -> AgentReply:
        """Add one patient utterance and return the secretary's answer."""
        async with conv.lock:
            content = [{"type": "text", "text": user_text}]
            if not conv.messages:
                content.insert(0, {"type": "text", "text": self._context_block(conv)})
            conv.messages.append({"role": "user", "content": content})
            try:
                return await self._run(conv)
            except anthropic.APIError:
                log.exception("Claude API call failed (conversation %s)", conv.id)
                return AgentReply(FALLBACK_REPLY)

    # ---- agent loop ---------------------------------------------------

    async def _create(self, messages: list):
        kwargs = dict(
            model=self.settings.claude_model,
            max_tokens=16000,
            system=self.system,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": self.settings.claude_effort},
            cache_control={"type": "ephemeral"},
        )
        if self.settings.claude_fallbacks:
            # On a safety-classifier refusal, the API retries on a fallback model.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return await self.client.beta.messages.create(**kwargs)

    async def _run(self, conv: Conversation) -> AgentReply:
        end_call = False
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._create(conv.messages)
            # Append the full content unchanged (thinking blocks included) so the
            # history stays append-only.
            conv.messages.append({"role": "assistant", "content": response.content})
            text = " ".join(b.text for b in response.content if b.type == "text").strip()

            if response.stop_reason == "refusal":
                return AgentReply(text or FALLBACK_REPLY)
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                conv.ended = conv.ended or end_call
                return AgentReply(text or FALLBACK_REPLY, end_call=end_call)

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "end_call":
                    end_call = True
                output, is_error = self._execute(conv, block.name, block.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(output, ensure_ascii=False),
                        "is_error": is_error,
                    }
                )
            conv.messages.append({"role": "user", "content": results})
        log.warning("Conversation %s exceeded %d tool rounds", conv.id, MAX_TOOL_ROUNDS)
        return AgentReply(FALLBACK_REPLY, end_call=end_call)

    # ---- tools --------------------------------------------------------

    def _execute(self, conv: Conversation, name: str, args: dict) -> tuple[dict, bool]:
        try:
            return self._dispatch(conv, name, args), False
        except BookingError as e:
            return {"error": str(e)}, True
        except (ValueError, KeyError, TypeError) as e:
            return {"error": f"invalid input: {e}"}, True

    def _dispatch(self, conv: Conversation, name: str, args: dict) -> dict:
        s = self.scheduler
        if name == "check_availability":
            day = date.fromisoformat(args["date"])
            slots = s.available_slots(day)
            result = {"date": day.isoformat(), "weekday": day.strftime("%A"), "free_slots": [_slot_view(x) for x in slots]}
            if not slots:
                result["next_available_days"] = [
                    {"date": d.isoformat(), "weekday": d.strftime("%A"), "free_slots": [_slot_view(x) for x in sl[:6]]}
                    for d, sl in s.next_available_days(day + timedelta(days=1), max_days=3)
                ]
            return result
        if name == "book_appointment":
            appt = s.book(
                patient_name=args["patient_name"],
                phone=args["phone"],
                start=datetime.strptime(args["start"], FMT),
                reason=args.get("reason") or None,
                channel=conv.channel,
            )
            return {"booked": True, "appointment_id": appt.id, "start": appt.start, "end": appt.end}
        if name == "find_appointments":
            appts = s.upcoming_for_phone(args["phone"])
            return {
                "appointments": [
                    {"appointment_id": a.id, "patient_name": a.patient_name, "start": a.start,
                     "weekday": datetime.strptime(a.start, FMT).strftime("%A"), "reason": a.reason}
                    for a in appts
                ]
            }
        if name == "cancel_appointment":
            appt = s.cancel(int(args["appointment_id"]), args["phone"])
            return {"cancelled": True, "appointment_id": appt.id, "start": appt.start}
        if name == "end_call":
            return {"ok": True}
        raise ValueError(f"unknown tool {name}")

"""Agent loop tests with a scripted stand-in for the Claude API (no network)."""

from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app.agent import SecretaryAgent
from app.main import create_app


def text(t):
    return NS(type="text", text=t)


def tool(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


class ScriptedClient:
    """Returns canned responses in order and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        content, stop = self.responses.pop(0)
        return NS(content=content, stop_reason=stop)


@pytest.fixture
def make_agent(settings, scheduler):
    def make(responses):
        return SecretaryAgent(scheduler, settings, client=ScriptedClient(responses))
    return make


async def test_books_through_tools(make_agent, scheduler):
    agent = make_agent([
        ([tool("t1", "check_availability", {"date": "2026-10-13"})], "tool_use"),
        ([tool("t2", "book_appointment", {"patient_name": "Karim", "phone": "0612345678",
                                          "start": "2026-10-13T09:00", "reason": ""})], "tool_use"),
        ([text("تم حجز موعدك يوم الثلاثاء على الساعة التاسعة.")], "end_turn"),
    ])
    conv = agent.start("phone", caller_phone="0612345678")
    reply = await agent.reply(conv, "نعم، أكد الحجز")

    assert "تم حجز" in reply.text and not reply.end_call
    [appt] = scheduler.upcoming_for_phone("0612345678")
    assert appt.channel == "phone" and appt.start == "2026-10-13T09:00"

    reqs = agent.client.requests
    first_user = reqs[0]["messages"][0]["content"]
    assert "caller phone number: 0612345678" in first_user[0]["text"]
    avail = reqs[1]["messages"][-1]["content"][0]
    assert avail["tool_use_id"] == "t1" and "2026-10-13T09:00" in avail["content"]


async def test_tool_errors_are_reported_to_model(make_agent, scheduler):
    scheduler.book("Taken", "0700000000", datetime(2026, 10, 13, 9), None, "web")
    agent = make_agent([
        ([tool("t1", "book_appointment", {"patient_name": "Karim", "phone": "0612345678",
                                          "start": "2026-10-13T09:00", "reason": ""})], "tool_use"),
        ([text("عذراً، هذا الموعد محجوز.")], "end_turn"),
    ])
    conv = agent.start("phone")
    await agent.reply(conv, "احجز لي")
    result = agent.client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "محجوز" in result["content"]


async def test_end_call(make_agent):
    agent = make_agent([
        ([text("مع السلامة."), tool("t1", "end_call", {})], "tool_use"),
        ([text("")], "end_turn"),
    ])
    conv = agent.start("phone")
    reply = await agent.reply(conv, "شكراً، مع السلامة")
    assert reply.end_call and conv.ended


def test_voice_gateway_endpoints(settings, scheduler, make_agent):
    agent = make_agent([([text("أي يوم يناسبك؟")], "end_turn")])
    client = TestClient(create_app(settings, agent=agent, scheduler=scheduler))
    start = client.post("/voice/calls", json={"caller_phone": "0612345678"}).json()
    assert start["reply"] and start["call_id"]
    turn = client.post(f"/voice/calls/{start['call_id']}/turn", json={"text": "بغيت موعد"}).json()
    assert turn == {"call_id": start["call_id"], "reply": "أي يوم يناسبك؟", "end_call": False}
    assert client.post("/voice/calls/nope/turn", json={"text": "x"}).status_code == 404

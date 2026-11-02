from __future__ import annotations

import asyncio

from callgate import ApprovalBroker, ApprovalRequest


def test_resolve_unknown_id_returns_none() -> None:
    assert ApprovalBroker(1).resolve("nope", True) is None


def test_request_waits_for_resolution_from_another_task() -> None:
    async def scenario() -> tuple[bool, int]:
        broker = ApprovalBroker(5)
        sent: list[ApprovalRequest] = []

        async def send(req: ApprovalRequest) -> None:
            sent.append(req)

        waiter = asyncio.create_task(broker.request("mcp__x__send", "{}", "because", send))
        while not sent:
            await asyncio.sleep(0)
        pending = len(broker.pending())
        assert broker.resolve(sent[0].id, True) is not None
        assert broker.resolve(sent[0].id, False) is None  # second tap is ignored
        return await waiter, pending

    approved, pending = asyncio.run(scenario())
    assert approved is True and pending == 1


def test_send_failure_denies() -> None:
    async def scenario() -> bool:
        broker = ApprovalBroker(5)

        async def send(req: ApprovalRequest) -> None:
            raise RuntimeError("telegram down")

        try:
            return await broker.request("mcp__x__send", "{}", "r", send)
        except RuntimeError:
            return False

    assert asyncio.run(scenario()) is False


def test_ids_are_unguessable_and_unique() -> None:
    async def scenario() -> set[str]:
        broker = ApprovalBroker(0.01)
        ids: set[str] = set()

        async def send(req: ApprovalRequest) -> None:
            ids.add(req.id)

        for _ in range(20):
            await broker.request("t", "{}", "r", send)
        return ids

    ids = asyncio.run(scenario())
    assert len(ids) == 20 and all(len(i) >= 12 for i in ids)


def test_deny_all_answers_every_pending_request() -> None:
    async def scenario() -> list[bool]:
        broker = ApprovalBroker(5)
        sent: list[ApprovalRequest] = []

        async def send(req: ApprovalRequest) -> None:
            sent.append(req)

        waiters = [asyncio.create_task(broker.request(f"t{i}", "{}", "r", send)) for i in range(3)]
        while len(sent) < 3:
            await asyncio.sleep(0)
        assert broker.deny_all() == 3
        return list(await asyncio.gather(*waiters))

    assert asyncio.run(scenario()) == [False, False, False]

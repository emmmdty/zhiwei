"""Fake external ticket service for testing tool execution flows."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from zhiwei.contracts.identifiers import new_id


@dataclass
class TicketRecord:
    ticket_id: UUID
    title: str
    status: str = "open"


class FakeTicketService:
    """In-memory ticket service simulating an external system."""

    def __init__(self) -> None:
        self._tickets: dict[UUID, TicketRecord] = {}
        self._call_log: list[dict[str, object]] = []

    def create_ticket(self, title: str) -> TicketRecord:
        ticket = TicketRecord(ticket_id=new_id(), title=title)
        self._tickets[ticket.ticket_id] = ticket
        self._call_log.append({"op": "create", "title": title})
        return ticket

    def close_ticket(self, ticket_id: UUID) -> TicketRecord:
        ticket = self._get(ticket_id)
        updated = TicketRecord(
            ticket_id=ticket.ticket_id,
            title=ticket.title,
            status="closed",
        )
        self._tickets[ticket_id] = updated
        self._call_log.append({"op": "close", "ticket_id": ticket_id})
        return updated

    def get_ticket(self, ticket_id: UUID) -> TicketRecord:
        return self._get(ticket_id)

    def _get(self, ticket_id: UUID) -> TicketRecord:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise KeyError(f"Ticket {ticket_id} not found")
        return ticket

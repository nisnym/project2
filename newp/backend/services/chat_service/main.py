"""chat-service (:9005)

The advisor. It owns no data and no analysis: it fetches the same snapshot the
UI renders, hands it to GitHub Models as grounding, and returns the answer with
the reasoning attached.

    snapshot (insight-service) ─┐
                                ├─→ prompt.py ─→ GitHub Models ─→ parsed answer
    suggestions (budget-service)┘                     │
                                                      └─ on any failure ─→ fallback.py

The fallback is not a stub — it answers from the same snapshot, so switching
CHAT_PROVIDER between `github_sdk`, `github_cli` and `fallback` changes who
composes the sentence, never which facts are allowed into it.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Query

from shared.config import settings
from shared.errors import AppError
from shared.http import ServiceClient
from shared.models import ChatRequest, ChatResponse
from shared.service import create_app

from . import fallback
from .prompt import build_messages, parse_model_reply, render_facts
from .providers import ProviderUnavailable, get_provider, provider_report

log = logging.getLogger("chat-service")

insight_client = ServiceClient("insight", settings.insight_service_url)
budget_client = ServiceClient("budget", settings.budget_service_url)


async def _close_clients() -> None:
    await asyncio.gather(insight_client.aclose(), budget_client.aclose())


app = create_app(
    name="chat-service",
    title="PFA · chat-service",
    description="The AI advisor: GitHub Models grounded on the customer's own snapshot.",
    on_shutdown=_close_clients,
)


def _resolve_customer_id(body: ChatRequest) -> str:
    customer_id = body.customerId or (body.customer or {}).get("id")
    if not customer_id:
        raise AppError(
            "I need to know whose account this is before I can answer.",
            code="customer_required",
            hint='Send {"customerId": "cust-001", "message": "..."}.',
        )
    return str(customer_id)


async def _load_snapshot(customer_id: str) -> dict:
    """Facts first. Suggestions are a bonus — a failure there isn't fatal."""
    snapshot, suggestions = await asyncio.gather(
        insight_client.get(f"/insights/{customer_id}/snapshot"),
        _quietly(budget_client.get(f"/budgets/{customer_id}/suggestions")),
    )
    snapshot["budgetSuggestions"] = suggestions or []
    return snapshot


async def _quietly(coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        log.warning("optional upstream call failed: %s", exc)
        return None


@app.post("/chat", response_model=ChatResponse, tags=["chat"])
async def chat(body: ChatRequest) -> ChatResponse:
    customer_id = _resolve_customer_id(body)

    try:
        snapshot = await _load_snapshot(customer_id)
    except Exception as exc:  # noqa: BLE001
        # Ungrounded is worse than unavailable. Say so rather than improvise.
        log.error("could not load snapshot for %s: %s", customer_id, exc)
        return ChatResponse(
            answer=(
                "I can't reach your account data at the moment, so I'm not going to guess. "
                "Try again in a few seconds — everything else on this page is still live."
            ),
            reasoning=f"insight-service did not answer: {exc}",
            confidence="low",
            intent="unavailable",
            source="unavailable",
        )

    provider = get_provider()
    if provider is not None:
        messages = build_messages(snapshot, body.message, [t.model_dump() for t in body.history])
        try:
            raw = await provider.complete(messages)
            parsed = parse_model_reply(raw)
            return ChatResponse(
                answer=parsed.get("answer", ""),
                reasoning=parsed.get("reasoning"),
                data_points_used=parsed.get("data_points_used", []) or [],
                confidence=parsed.get("confidence", "medium"),
                intent=parsed.get("intent", "unknown"),
                source=provider.name,
                model=settings.github_model,
            )
        except ProviderUnavailable as exc:
            log.warning("%s unusable (%s) — answering deterministically", provider.name, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s failed mid-answer: %s", provider.name, exc)

    answer = fallback.answer(snapshot, body.message)
    return ChatResponse(
        answer=answer["answer"],
        reasoning=answer["reasoning"],
        data_points_used=answer["data_points_used"],
        confidence=answer["confidence"],
        intent=answer["intent"],
        source="fallback",
    )


@app.get("/chat/providers", tags=["chat"])
async def providers() -> dict:
    """What the advisor will actually use, and why — check before demoing."""
    return {
        "configured": settings.chat_provider,
        "model": settings.github_model,
        "endpoint": settings.github_models_base_url,
        "providers": provider_report(),
    }


@app.get("/chat/prompt-preview", tags=["chat"])
async def prompt_preview(
    customerId: str = Query(...),
    message: str = Query("How am I doing this month?"),
) -> dict:
    """The exact prompt this customer's question would produce.

    Here so "what did you send the model?" has a URL for an answer instead of
    a promise.
    """
    snapshot = await _load_snapshot(customerId)
    return {
        "customerId": customerId,
        "message": message,
        "facts": render_facts(snapshot),
        "messages": build_messages(snapshot, message, []),
    }


@app.get("/meta/stats", tags=["meta"])
async def stats() -> dict:
    return {
        "provider": settings.chat_provider,
        "model": settings.github_model,
        "grounding": "insight-service /snapshot",
        "dependsOn": {
            "insight-service": settings.insight_service_url,
            "budget-service": settings.budget_service_url,
        },
    }

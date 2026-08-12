"""Transports to GitHub Models.

Two ways in, both pointed at the same models, chosen with CHAT_PROVIDER:

* ``github_sdk`` — the OpenAI-compatible SDK against
  ``https://models.github.ai/inference`` with a GitHub token. This is the one
  to ship: it is async, it streams if you want it to later, and it returns
  structured JSON.
* ``github_cli`` — shells out to ``gh models run``. Nothing to configure
  beyond being logged into ``gh``, which makes it the quickest way to prove
  the wiring on a laptop that has no token in its environment.

Both return raw model text; parsing and grounding live in prompt.py, and the
deterministic answer engine in fallback.py takes over if a provider is not
usable. Swapping provider changes no other file.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any, Optional, Protocol

from shared.config import settings

log = logging.getLogger("chat-service.providers")


class ProviderUnavailable(RuntimeError):
    """Raised when a provider can't run — caller falls back rather than 500s."""


class ChatProvider(Protocol):
    name: str

    def availability(self) -> tuple[bool, str]: ...

    async def complete(self, messages: list[dict[str, Any]]) -> str: ...


# ----------------------------------------------------------------------
# GitHub Models — SDK
# ----------------------------------------------------------------------

class GitHubModelsSDK:
    """GitHub Models over the OpenAI wire protocol."""

    name = "github_sdk"

    def __init__(self) -> None:
        self._client: Optional[Any] = None

    def availability(self) -> tuple[bool, str]:
        if not settings.github_token:
            return False, "GITHUB_TOKEN is not set (a token with the models scope is required)."
        try:
            import openai  # noqa: F401
        except ImportError:
            return False, "The openai package is not installed — `uv sync` in backend/."
        return True, f"Ready · {settings.github_model} via {settings.github_models_base_url}"

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=settings.github_models_base_url,
                api_key=settings.github_token,
                timeout=settings.chat_timeout_seconds,
                max_retries=1,
            )
        return self._client

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        ok, why = self.availability()
        if not ok:
            raise ProviderUnavailable(why)

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": settings.github_model,
            "messages": messages,
            "temperature": settings.chat_temperature,
            "max_tokens": settings.chat_max_tokens,
        }

        try:
            # Ask for JSON natively where the model supports it; some models
            # behind GitHub Models don't, and reject the parameter outright.
            response = await client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except Exception as exc:  # noqa: BLE001
            if "response_format" not in str(exc):
                raise ProviderUnavailable(f"GitHub Models call failed: {exc}") from exc
            log.info("Model %s rejected response_format; retrying without it", settings.github_model)
            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception as retry_exc:  # noqa: BLE001
                raise ProviderUnavailable(f"GitHub Models call failed: {retry_exc}") from retry_exc

        return (response.choices[0].message.content or "").strip()


# ----------------------------------------------------------------------
# GitHub Models — CLI
# ----------------------------------------------------------------------

class GitHubModelsCLI:
    """`gh models run` — same models, no token plumbing.

    Requires the extension once: `gh extension install github/gh-models`.
    Flags have shifted between versions of that extension; if yours differs,
    this is the only place to change.
    """

    name = "github_cli"

    def availability(self) -> tuple[bool, str]:
        if shutil.which("gh") is None:
            return False, "The `gh` CLI is not on PATH."
        return True, f"Ready · `gh models run {settings.github_model}`"

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        ok, why = self.availability()
        if not ok:
            raise ProviderUnavailable(why)

        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        conversation = "\n\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages if m["role"] != "system"
        )

        argv = ["gh", "models", "run", settings.github_model, "--system-prompt", system]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(conversation.encode()),
                timeout=settings.chat_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderUnavailable(
                f"`gh models run` did not return within {settings.chat_timeout_seconds}s."
            ) from exc
        except OSError as exc:
            raise ProviderUnavailable(f"Could not start `gh`: {exc}") from exc

        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or f"exit {process.returncode}"
            raise ProviderUnavailable(f"`gh models run` failed: {detail}")

        return stdout.decode(errors="replace").strip()


# ----------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------

_PROVIDERS: dict[str, ChatProvider] = {
    GitHubModelsSDK.name: GitHubModelsSDK(),
    GitHubModelsCLI.name: GitHubModelsCLI(),
}


def get_provider(name: Optional[str] = None) -> Optional[ChatProvider]:
    """None means "use the deterministic engine" — a valid, configured choice."""
    return _PROVIDERS.get((name or settings.chat_provider).strip().lower())


def provider_report() -> list[dict]:
    """What the operator sees on GET /chat/providers before going on stage."""
    rows = []
    for name, provider in _PROVIDERS.items():
        ok, detail = provider.availability()
        rows.append(
            {
                "name": name,
                "usable": ok,
                "detail": detail,
                "selected": name == settings.chat_provider,
            }
        )
    rows.append(
        {
            "name": "fallback",
            "usable": True,
            "detail": "Deterministic rule engine over the same snapshot. No network, always available.",
            "selected": settings.chat_provider not in _PROVIDERS,
        }
    )
    return rows

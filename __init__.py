"""hermes-plugin-credits — agent tool + slash command registration.

Dashboard UI lives under ``dashboard/`` (manifest name ``credits``).
This module adds the Hermes-native agent surface:

* tool ``credits_status`` — structured sanitized balances for the model
* slash ``/credits`` — human-friendly markdown summary in chat
"""

from __future__ import annotations

import logging

from .agent_tools import (
    CREDITS_STATUS_SCHEMA,
    credits_available,
    handle_credits_command,
    handle_credits_status,
)

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register agent tool + slash command. Called when plugin is enabled."""
    ctx.register_tool(
        name="credits_status",
        toolset="credits",
        schema=CREDITS_STATUS_SCHEMA,
        handler=handle_credits_status,
        check_fn=credits_available,
        is_async=False,  # handler bridges async probe via model_tools._run_async
        description=(
            "Fetch sanitized provider credit/balance status. "
            "Prefer this over DIY OpenRouter/BWS key probes."
        ),
        emoji="💳",
    )
    ctx.register_command(
        "credits",
        handle_credits_command,
        description="Show provider credit/balance summary (sanitized)",
        args_hint="[refresh] [provider]",
    )
    logger.info("credits plugin: registered credits_status tool and /credits command")

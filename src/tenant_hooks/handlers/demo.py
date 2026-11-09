import logging

from tenant_hooks.handlers.registry import HandlerContext, HandlerRegistry

logger = logging.getLogger(__name__)


def log_event(context: HandlerContext) -> None:
    logger.info(
        "handled %s for tenant %s (event %s, attempt %d)",
        context.event_type,
        context.tenant_id,
        context.event_id,
        context.attempt,
    )


def build_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register("log_event", log_event)
    return registry

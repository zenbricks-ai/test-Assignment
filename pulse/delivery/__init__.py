"""Delivery channel factory. New channel = new module + one line in _REGISTRY."""

from collections.abc import Callable

from pulse.config import Settings
from pulse.delivery.base import DeliveryChannel
from pulse.delivery.mail import EmailChannel
from pulse.delivery.web import WebChannel

_REGISTRY: dict[str, Callable[[Settings], DeliveryChannel]] = {
    "web": lambda s: WebChannel(s.public_base_url),
    "email": lambda s: EmailChannel(s),
}


def create_channel(channel_type: str, settings: Settings) -> DeliveryChannel:
    try:
        return _REGISTRY[channel_type](settings)
    except KeyError:
        raise ValueError(f"Unknown channel: {channel_type!r}") from None

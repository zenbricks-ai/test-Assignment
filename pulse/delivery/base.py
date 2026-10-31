from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DigestMessage:
    date: str
    profile_slug: str
    subject: str
    html: str
    text: str


@dataclass(frozen=True)
class DeliveryResult:
    status: str  # "sent" | "published"
    detail: str = ""


class DeliveryChannel(Protocol):
    def send(self, digest: DigestMessage, target: str) -> DeliveryResult: ...

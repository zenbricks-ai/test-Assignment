from pulse.delivery.base import DeliveryResult, DigestMessage


class WebChannel:
    """The digest row is already persisted and served by the API; publishing = its URL."""

    def __init__(self, public_base_url: str) -> None:
        self.base = public_base_url.rstrip("/")

    def send(self, digest: DigestMessage, target: str) -> DeliveryResult:
        return DeliveryResult(
            "published", f"{self.base}/digests/{digest.profile_slug}/{digest.date}"
        )

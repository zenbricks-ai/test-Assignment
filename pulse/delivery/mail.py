import smtplib
from email.message import EmailMessage

from pulse.config import Settings
from pulse.delivery.base import DeliveryResult, DigestMessage


class EmailChannel:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def send(self, digest: DigestMessage, target: str) -> DeliveryResult:
        msg = EmailMessage()
        msg["Subject"] = digest.subject
        msg["From"] = self.s.mail_from
        msg["To"] = target
        msg.set_content(digest.text)
        msg.add_alternative(digest.html, subtype="html")

        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
            if self.s.smtp_starttls:
                smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password.get_secret_value())
            smtp.send_message(msg)
        return DeliveryResult("sent", target)

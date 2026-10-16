"""Optional e-mail copy of security alerts (configured under "alert_email" in config.json)."""
import logging
import os
import smtplib
import threading
from email.message import EmailMessage

from . import settings as cfg

log = logging.getLogger("lifeguard.notify")


def notify_admins_email(subject, body):
    """Send in the background; silently skipped when e-mail isn't configured.

    The SMTP password may be supplied via LIFEGUARD_SMTP_PASSWORD instead of config.json.
    """
    conf = cfg.load_config().get("alert_email") or {}
    if not conf.get("smtp_host") or not conf.get("to"):
        return

    def send():
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = conf.get("from") or conf.get("username")
            msg["To"] = ", ".join(conf["to"]) if isinstance(conf["to"], list) else conf["to"]
            msg.set_content(body)
            with smtplib.SMTP(conf["smtp_host"], int(conf.get("smtp_port", 587)), timeout=15) as smtp:
                smtp.starttls()
                if conf.get("username"):
                    smtp.login(conf["username"],
                               os.environ.get("LIFEGUARD_SMTP_PASSWORD") or conf.get("password", ""))
                smtp.send_message(msg)
            log.info("security alert e-mail sent")
        except Exception as exc:
            log.error("security alert e-mail failed: %s", exc)

    threading.Thread(target=send, daemon=True, name="alert-email").start()

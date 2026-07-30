"""Email channel — a minimal branded template for notification emails.

Deliberately self-contained: reuses the visual style of
app/service/email_service.py (brand colors #1565C0/#2196F3, 560px table
layout) via a generic helper here, without modifying that module. Only the
low-level `send_email` sender is reused.
"""
import html
import logging
import os

from app.authentication.models import User
from app.notification.channels.base import NotificationChannel
from app.notification.models import Notification
from app.service.email_service import send_email

logger = logging.getLogger(__name__)

# action_url values are FE routes ("/devices/42"); emails need absolute links.
DASHBOARD_BASE_URL = os.getenv(
    "DASHBOARD_BASE_URL", "https://mosquitosurveillancedashboard.website"
)


def _notification_email_html(
    first_name: str, title: str, body: str, action_url: str | None = None
) -> str:
    safe_first_name = html.escape(first_name or "there")
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    button_html = ""
    if action_url:
        href = f"{DASHBOARD_BASE_URL}{action_url}" if action_url.startswith("/") else action_url
        button_html = f"""
              <table role="presentation" cellspacing="0" cellpadding="0" style="margin: 0;">
                <tr>
                  <td style="background-color: #1565C0; border-radius: 8px;">
                    <a href="{html.escape(href, quote=True)}" style="display: inline-block; padding: 14px 28px; color: #ffffff; text-decoration: none; font-size: 16px; font-weight: 600;">View in Dashboard</a>
                  </td>
                </tr>
              </table>
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_title}</title>
</head>
<body style="margin:0; padding:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background-color: #E4E4E7;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #E4E4E7; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 560px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.08);">
          <tr>
            <td style="background: linear-gradient(135deg, #1565C0 0%, #2196F3 100%); padding: 32px 40px; text-align: center;">
              <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">Mosquito Dashboard</h1>
              <p style="margin: 8px 0 0; color: rgba(255,255,255,0.9); font-size: 14px;">Notification</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 40px;">
              <p style="margin: 0 0 16px; color: #000000; font-size: 18px; font-weight: 600;">Hi {safe_first_name},</p>
              <p style="margin: 0 0 12px; color: #000000; font-size: 16px; font-weight: 600; line-height: 1.6;">{safe_title}</p>
              <p style="margin: 0 0 24px; color: #000000; font-size: 16px; line-height: 1.6;">{safe_body}</p>
              {button_html}
              <p style="margin: 32px 0 0; color: #000000; font-size: 14px; line-height: 1.6;">You are receiving this because email notifications are enabled in your preferences.</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 24px 40px; background-color: #E4E4E7; border-top: 1px solid #E4E4E7;">
              <p style="margin: 0; color: #000000; font-size: 12px; opacity: 0.8;">© Mosquito Dashboard. All rights reserved.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def send_notification_email(
    to: str, first_name: str, title: str, body: str, action_url: str | None = None
) -> None:
    """Send one branded notification email. Raises on failure (the channel
    catches and reports it)."""
    send_email(
        to=to,
        subject=title,
        body=_notification_email_html(first_name, title, body, action_url),
    )


class EmailChannel(NotificationChannel):
    def send(self, notification: Notification, user: User) -> bool:
        try:
            send_notification_email(
                to=user.email,
                first_name=user.first_name,
                title=notification.title,
                body=notification.body,
                action_url=notification.action_url,
            )
            return True
        except Exception:
            logger.exception(
                "Email channel failed for notification %s user %s", notification.id, user.id
            )
            return False

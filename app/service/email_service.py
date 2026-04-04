import os
import resend
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM")
resend.api_key = RESEND_API_KEY


def _welcome_email_html(first_name: str) -> str:
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Welcome to Mosquito Surveillance Dashboard</title>
</head>
<body style="margin:0; padding:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background-color: #E4E4E7;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #E4E4E7; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 560px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.08);">
          <tr>
            <td style="background: linear-gradient(135deg, #1565C0 0%, #2196F3 100%); padding: 32px 40px; text-align: center;">
              <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">Mosquito Dashboard</h1>
              <p style="margin: 8px 0 0; color: rgba(255,255,255,0.9); font-size: 14px;">Welcome aboard</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 40px;">
              <p style="margin: 0 0 16px; color: #000000; font-size: 18px; font-weight: 600;">Hi {first_name},</p>
              <p style="margin: 0 0 24px; color: #000000; font-size: 16px; line-height: 1.6;">Thanks for signing up. Your account has been created successfully. You can now sign in and start using the dashboard.</p>
              <table role="presentation" cellspacing="0" cellpadding="0" style="margin: 0;">
                <tr>
                  <td style="background-color: #1565C0; border-radius: 8px;">
                    <a href="https://mosquitosurveillancedashboard.website/login" style="display: inline-block; padding: 14px 28px; color: #ffffff; text-decoration: none; font-size: 16px; font-weight: 600;">Get started</a>
                  </td>
                </tr>
              </table>
              <p style="margin: 32px 0 0; color: #000000; font-size: 14px; line-height: 1.6;">If you didn't create this account, you can safely ignore this email.</p>
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


def _researcher_request_email_html(first_name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Researcher Request Received</title>
</head>
<body style="margin:0; padding:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background-color: #E4E4E7;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #E4E4E7; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 560px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.08);">
          <tr>
            <td style="background: linear-gradient(135deg, #1565C0 0%, #2196F3 100%); padding: 32px 40px; text-align: center;">
              <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">Mosquito Dashboard</h1>
              <p style="margin: 8px 0 0; color: rgba(255,255,255,0.9); font-size: 14px;">Researcher Request Update</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 40px;">
              <p style="margin: 0 0 16px; color: #000000; font-size: 18px; font-weight: 600;">Hi {first_name},</p>
              <p style="margin: 0 0 24px; color: #000000; font-size: 16px; line-height: 1.6;">Your researcher request has been received and is currently being reviewed. We will notify you once a decision has been made.</p>
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


def _researcher_approved_email_html(first_name: str, cluster_uuid: str | None = None, cluster_password: str | None = None) -> str:
    credentials_html = ""
    if cluster_uuid and cluster_password:
        credentials_html = f"""
              <div style="margin: 28px 0 0; padding: 18px; border: 1px solid #E4E4E7; border-radius: 10px; background-color: #F8FAFC;">
                <p style="margin: 0 0 10px; color: #000000; font-size: 14px; font-weight: 700;">Your cluster details</p>
                <p style="margin: 0 0 8px; color: #000000; font-size: 14px; line-height: 1.6;"><strong>Cluster UUID:</strong> <span style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;">{cluster_uuid}</span></p>
                <p style="margin: 0; color: #000000; font-size: 14px; line-height: 1.6;"><strong>Cluster password:</strong> <span style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;">{cluster_password}</span></p>
                <p style="margin: 12px 0 0; color: #000000; font-size: 12px; line-height: 1.6; opacity: 0.8;">Keep this password secure. If you think it was exposed, contact support.</p>
              </div>
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Researcher Request Approved</title>
</head>
<body style="margin:0; padding:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background-color: #E4E4E7;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #E4E4E7; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 560px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.08);">
          <tr>
            <td style="background: linear-gradient(135deg, #1565C0 0%, #2196F3 100%); padding: 32px 40px; text-align: center;">
              <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">Mosquito Dashboard</h1>
              <p style="margin: 8px 0 0; color: rgba(255,255,255,0.9); font-size: 14px;">Researcher Request Approved</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 40px;">
              <p style="margin: 0 0 16px; color: #000000; font-size: 18px; font-weight: 600;">Hi {first_name},</p>
              <p style="margin: 0 0 24px; color: #000000; font-size: 16px; line-height: 1.6;">Great news! Your researcher request has been <strong>approved</strong>. You now have full researcher access to the Mosquito Surveillance Dashboard.</p>
              {credentials_html}
              <table role="presentation" cellspacing="0" cellpadding="0" style="margin: 0;">
                <tr>
                  <td style="background-color: #1565C0; border-radius: 8px;">
                    <a href="https://mosquitosurveillancedashboard.website/login" style="display: inline-block; padding: 14px 28px; color: #ffffff; text-decoration: none; font-size: 16px; font-weight: 600;">Go to Dashboard</a>
                  </td>
                </tr>
              </table>
              <p style="margin: 32px 0 0; color: #000000; font-size: 14px; line-height: 1.6;">If you have any questions, feel free to reach out to our support team.</p>
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


def _researcher_declined_email_html(first_name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Researcher Request Declined</title>
</head>
<body style="margin:0; padding:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background-color: #E4E4E7;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #E4E4E7; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 560px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.08);">
          <tr>
            <td style="background: linear-gradient(135deg, #1565C0 0%, #2196F3 100%); padding: 32px 40px; text-align: center;">
              <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">Mosquito Dashboard</h1>
              <p style="margin: 8px 0 0; color: rgba(255,255,255,0.9); font-size: 14px;">Researcher Request Update</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 40px;">
              <p style="margin: 0 0 16px; color: #000000; font-size: 18px; font-weight: 600;">Hi {first_name},</p>
              <p style="margin: 0 0 24px; color: #000000; font-size: 16px; line-height: 1.6;">Thank you for your interest. Unfortunately, your researcher request has been <strong>declined</strong> at this time. You may still use the dashboard with your current access level.</p>
              <p style="margin: 0 0 24px; color: #000000; font-size: 16px; line-height: 1.6;">If you believe this decision was made in error or would like more information, please contact our support team.</p>
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


def send_email(to: str, subject: str, body: str) -> None:
    try:
        params: resend.Emails.SendParams = {
            "from": EMAIL_FROM or "mosquito-dashboard@resend.dev",
            "to": to,
            "subject": subject,
            "html": body,
        }
        resend.Emails.send(params)
    except Exception as e:
        print(f"Error sending email: {e}")
        raise e


def send_welcome_email(to: str, first_name: str) -> None:
    """Send a welcome email after successful signup. Uses brand colors: primary #1565C0, secondary #2196F3, gray #E4E4E7."""
    html = _welcome_email_html(first_name)
    send_email(
        to=to,
        subject="Welcome to Mosquito Dashboard",
        body=html,
    )


def send_researcher_request_email(to: str, first_name: str) -> None:
    """Send a confirmation email when a researcher request is submitted."""
    html = _researcher_request_email_html(first_name)
    send_email(
        to=to,
        subject="Researcher Request Received",
        body=html,
    )


def send_researcher_approved_email(to: str, first_name: str, cluster_uuid: str | None = None, cluster_password: str | None = None) -> None:
    """Send a notification email when a researcher request is approved."""
    html = _researcher_approved_email_html(first_name, cluster_uuid=cluster_uuid, cluster_password=cluster_password)
    send_email(
        to=to,
        subject="Researcher Request Approved",
        body=html,
    )


def send_researcher_declined_email(to: str, first_name: str) -> None:
    """Send a notification email when a researcher request is declined."""
    html = _researcher_declined_email_html(first_name)
    send_email(
        to=to,
        subject="Researcher Request Declined",
        body=html,
    )

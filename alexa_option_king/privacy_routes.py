from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Option King Privacy"])


@router.get("/privacy", response_class=HTMLResponse)
def option_king_privacy_policy():
    return HTMLResponse(
        """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>Option King AI Privacy Policy</title>
  <style>
    body{font-family:Arial,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;line-height:1.6;color:#1f2937}
    h1,h2{color:#111827} .muted{color:#6b7280}
  </style>
</head>
<body>
  <h1>Option King AI Privacy Policy</h1>
  <p class=\"muted\">Last updated: 28 August 2026</p>
  <p>Option King AI provides read-only voice access to trading information available in the user's Option King account.</p>
  <h2>Information we use</h2>
  <p>When a user links an Option King account with Alexa, we use the account-linking token provided through Alexa to identify the linked Option King user and return that user's permitted information, such as current positions, P&amp;L, recent trade details, bot status and AI signal information.</p>
  <h2>How information is used</h2>
  <p>Linked-account information is used only to authenticate the user, keep users separated from one another, and answer requested Alexa skill queries. The Alexa skill does not place, modify or execute trades.</p>
  <h2>Sharing</h2>
  <p>We do not sell personal information. Information is shared only with service providers necessary to operate the Option King service and Alexa integration, or when required by law.</p>
  <h2>Security and retention</h2>
  <p>We use reasonable technical safeguards to protect account information. Account-linking tokens are used for authentication and access control. Users may unlink the skill from the Alexa app to stop Alexa access.</p>
  <h2>Children</h2>
  <p>Option King AI is not directed to children under 13.</p>
  <h2>Financial disclaimer</h2>
  <p>Market and trading information may be delayed or subject to data availability. Information provided by the skill is informational and is not financial advice.</p>
  <h2>Contact</h2>
  <p>For privacy questions, contact the Option King AI administrator through the support details provided in the Option King service.</p>
</body>
</html>"""
    )

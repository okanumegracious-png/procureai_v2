# Gmail API supplier email setup

ProcureAI can send supplier request emails from the organisation's existing Gmail mailbox without purchasing a domain. The Gmail API uses HTTPS, so it does not depend on outbound SMTP ports.

## Railway variables

Set these on the `procureai_v2` service in the production environment:

- `EMAIL_PROVIDER=gmail`
- `GMAIL_SENDER_EMAIL=okanumegracious@gmail.com`
- `GMAIL_CLIENT_ID` — OAuth 2.0 client ID from Google Cloud
- `GMAIL_CLIENT_SECRET` — matching OAuth client secret
- `GMAIL_REFRESH_TOKEN` — refresh token authorized for the Gmail send scope

Treat the client secret and refresh token as passwords. Add them only in Railway Variables; never commit them or paste them into chat.

## One-time Google setup

1. Open Google Cloud Console and create a project for ProcureAI.
2. In APIs & Services → Library, enable **Gmail API**.
3. Configure the OAuth consent screen. Choose External if prompted, add `okanumegracious@gmail.com` as a test user, and keep the app in Testing while evaluating.
4. Create an OAuth Client ID of type **Web application**.
5. Add this exact Authorized redirect URI: `https://developers.google.com/oauthplayground`.
6. Open Google OAuth 2.0 Playground. Open its settings (gear), enable **Use your own OAuth credentials**, and enter the client ID and client secret from the previous step.
7. In Step 1, enter this scope: `https://www.googleapis.com/auth/gmail.send`, then authorize using `okanumegracious@gmail.com`.
8. In Step 2, exchange the authorization code for tokens. Copy the **refresh token** from the response.
9. Add the four Gmail variables above to Railway. Keep `EMAIL_PROVIDER=gmail` so the application does not attempt the Resend or SMTP routes.
10. Redeploy the Railway service, then send a supplier request to an address you control as a delivery test.

Google may display an unverified-app warning while the OAuth consent screen is in Testing. Only authorize the app you just created and only while signed into the intended mailbox. The Gmail send scope permits ProcureAI to send mail as that account.

## Runtime behavior

For each supplier request, ProcureAI exchanges the refresh token for a short-lived access token and sends the message through `users.messages.send`. Tokens are read from Railway environment variables and are not written to the ProcureAI database. Provider errors are returned to the UI without exposing OAuth credentials.

A successful API response means Google accepted the message for sending; confirm delivery in Gmail's Sent folder and with the recipient during the first live test.

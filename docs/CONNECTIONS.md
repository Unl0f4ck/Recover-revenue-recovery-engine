# Your Razorpay test account and customer messages

## Connect

1. Start `python -m scripts.serve` and open http://127.0.0.1:8000.
2. Open **Connections**. In your Razorpay dashboard, select Test mode and obtain
   a test Key ID and Key Secret from API Keys in account settings.
3. Give the workspace a name and enter both keys. Optionally enter a webhook
   signing secret matching the webhook you will configure in Razorpay.
4. Click **Verify & connect**. The server authenticates using a bounded GET to
   `/v1/payments?count=1`; there are no external writes during connection.
5. Use **Batch lab → Preview account**, then **Create test links** when ready.

Only `rzp_test_` keys are accepted. Invalid or unreachable credentials leave the
previous connection unchanged. Secrets are never returned in API responses,
validation errors, browser storage or project files. They live in process memory.
The HttpOnly, SameSite=Strict session expires after 8 hours; HTTPS connections use
a Secure cookie. Remote connections require HTTPS and the server operator token.

Data lives under `data/console/accounts/<SHA256-of-verified-key-id>/`.
Credentials are discarded on disconnect or server restart. Audit data remains;
reconnect with the same verified test key to reopen it. A rotated Key ID creates
a different workspace. This is key-scoped separation, not a Razorpay merchant-ID
mapping or a full SaaS identity system. Multiple tabs in one browser share the
same connection cookie; use separate browser profiles for simultaneous accounts.
Use one server process/worker. Do not publicly expose the operator console
without proper authentication, retention controls and deployment hardening.

Imported CSV jobs, synthetic runs, case access, reply proposals, exports,
notification logs and live gateway calls are scoped to the active workspace.
The localhost server workspace remains available when no account is connected.
Remote clients cannot read or execute against the server's default live ledger.

## Send SMS and email

The integration uses Razorpay's native Payment Link notifications, not Gmail,
WhatsApp, an SMTP relay or a custom email-marketing provider. No second API key is
required. Razorpay controls notification content and delivery.

- For new recovery links: enable **Request email notifications** and/or **Request
  SMS notifications** in Connections, then run **Create test links** in Batch lab.
  The confirmation explicitly includes permission to contact the recipients.
- For an existing recovery link: open its case from the test ledger and choose
  **Send email** or **Send SMS**. This requests a notification for the existing
  link; it does not create another link.
- Review **Messages** for the latest state of each reference/attempt/channel.
  No SMS/email selection means no notification request. Preview always stays
  dry-run even when a channel checkbox is enabled.

An email-identified case supports email; a phone-identified case supports SMS.
The current engine keeps one primary contact per case. It does not synthesize a
missing contact or automatically send both channels to the same customer.
Local redirect recipients are disabled for web-console actions. Missing contact
details require fixing the upstream account data, not entering arbitrary send-to
addresses in this console.

Manual notification requests check the original obligation and link immediately
before sending, match the provider recipient to the case, and honor opt-outs,
promises, quiet hours, the global kill switch and contact ceilings. A manual send
also constrains the next automated rung's minimum interval. Requests require
explicit confirmation. These checks do not prove legal consent; operators must
only use customers/test recipients they are authorized to contact.

The durable notification claim is written before the provider POST. A repeated
click cannot resend the same link/attempt/channel. On a lost response the state
is **unknown**, and automatic resend is held. Verify with Razorpay rather than
deleting the journal. **Requested** means a provider request, not confirmed
delivery and never payment recovery. Existing campaign `DELIVERED` states denote
the link-creation outcome; use the Messages log for notification state.

Test mode concerns payments, not permission to contact real people. Sandbox
limits or account restrictions may prevent sending. The implementation is
verified with mocked provider responses; no real inbox/handset delivery has
been claimed or performed as part of this change.

## Account-scoped callbacks

Connections displays `/webhooks/razorpay/<workspace-hash>`. Prefix it with your
public HTTPS origin and configure that URL in Razorpay, using the same signing
secret entered when connecting. Only raw-body HMAC-verified events enter that
workspace's ledger. The account must have an active connection; after expiration
or restart, reconnect so Razorpay retries can be processed. Legacy server `.env`
callbacks continue to use `/webhooks/razorpay`.

Legacy signed `/opt-out` and `/update-instrument` URLs remain server-workspace
features; they are not automatically inserted into Razorpay's native template.
For connected accounts, operators can record STOP replies on the corresponding
case to suppress further recovery. Customer-facing inbound messaging, custom
email templates, delivery receipts and persistent secret storage are not part
of this native-notification integration.

## Provider reference

- [Payment Link creation and notify fields](https://razorpay.com/docs/api/payments/payment-links/create-standard/)
- [Send or resend a Payment Link notification](https://razorpay.com/docs/api/payments/payment-links/resend/)
- [Payment Links overview](https://razorpay.com/docs/payments/payment-links/)

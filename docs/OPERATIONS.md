# Running Recover

## Local setup

```powershell
python -m pip install -e ".[dev]"
python -m scripts.serve --port 8000
```

Open http://127.0.0.1:8000. The batch lab and CSV preview work with no keys.
Existing `.env` is preserved. If creating it for the first time, use the supplied
`.env.example` and fill values locally. Never put the Razorpay secret in a UI file.

The project requires Python 3.11+. `requirements-dev.txt` pins versions used for
verification; install that file for the tested environment.

## Test-account actions

```powershell
python -m scripts.run_dunning --csv samples/failures.csv --unit rupees --horizon 14
python -m scripts.run_dunning --execute --max 5
python -m scripts.run_dunning --reconcile
python -m scripts.ops capabilities
```

The first command creates only preview records. `--execute` creates real
Razorpay TEST links. `--execute --notify` also requests outbound delivery: use
only with contacts you intend to message and inspect redirect settings first.
The web console now supports explicit SMS/email delivery controls in Connections,
and one-time notification requests for existing case links. See
[Connections and messaging](CONNECTIONS.md). Connecting an account never sends.

An imported preview does not establish that a CSV obligation is still unpaid.
Live CSV execution verifies recognizable Razorpay IDs and holds unsupported
external IDs. Such books need their own verified adapter before live collection.

`--horizon` and `--as-of` are never valid with `--execute`. Preview notifications
use separate ledgers. Back up the operation database and campaign ledger as a
unit; deleting the journal removes the local protection against duplicate creates.

## Webhooks

Set `RAZORPAY_WEBHOOK_SECRET` and configure the provider to POST to:

```text
https://<your-host>/webhooks/razorpay
```

Use an HTTPS deployment/tunnel you control. Subscribe to payment-link paid,
cancelled and expired events, and original order/payment settlement events.
The endpoint verifies the exact bytes before parsing and uses the previous
secret when `RAZORPAY_WEBHOOK_PREVIOUS_SECRET` is set. Duplicate events are
ignored. A busy writer returns a retryable response.

## Customer controls

Set a strong `RECOVERY_TOKEN_SECRET` and `RECOVERY_PUBLIC_URL` to the public
origin. Then issue a link:

```powershell
python -m scripts.ops link customer@example.com --purpose opt_out
python -m scripts.ops link customer@example.com --purpose update_instrument --reference order_example
```

The opt-out page records the customer's preference only after POST confirmation.
The update page directs an authorized customer to the latest associated secure
payment link; it never stores card details. It does not implement card vaulting
or saved-mandate enrollment. Links are operator-issued; provider-generated
notifications do not automatically include an unsubscribe footer.

## Operator access

Loopback access uses same-origin/custom-header protection. Set
`RECOVERY_ADMIN_TOKEN` to a random secret of at least 32 characters to also require
a token locally, and before any remote use. Enter it through **Operator access**
in the console. It stays in the browser's session storage. Set `RECOVERY_HOSTS`
to explicit hostnames, never `*`. Add TLS and an authenticated reverse proxy for
deployment. The sample Docker image is single-worker and requires persistent data.

## Troubleshooting

- **Unknown create outcome:** do not delete the claim or repeat the POST by hand.
  Check the provider reference and reconcile. An unresolved operation stays held.
- **No AI available:** clear STOP messages still suppress; other replies become
  human review. Configure `GEMINI_API_KEY` for natural-language interpretation.
- **Subscriptions unavailable:** the adapter currently has no saved-instrument
  executor. Use the labeled simulator to demonstrate the mandate ladder.
- **Batch interrupted:** restart and run a new batch. Completed runs remain intact.
- **Link limit reached:** test accounts may restrict available Payment Links.
  Inspect the account; do not repeatedly retry a permanent capacity error.

## Verification

```powershell
python -m pytest -q
python -m scripts.ops capabilities
node --check ui/control/app.js
```

Regression coverage includes transport ambiguity, provider reconciliation,
settlement through orders, stable plans, exposure bounds, preview isolation,
CSV-to-case ingestion, exact reply proposal application, signed HTTP callbacks
and customer opt-out. Full visual browser verification must also be performed
before recording the final pitch.

To export a completed synthetic run (ID from the batch API/status or source
selector), use `python -m scripts.export_run <run-id> --output artifacts/evidence.zip`.
The ZIP contains labeled metrics, run configuration, ledgers and SHA-256 hashes.
It refuses imported merchant previews and will not overwrite an existing bundle.

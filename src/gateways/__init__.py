"""Gateway adapters. One interface, one implementation, room for a second.

recoup, getpaidhq and emp-billing all isolate provider logic behind a single
interface, and all three give the same reason: decline codes, error vocabularies
and capability sets differ per provider, and if that difference leaks upward
then the recovery logic quietly becomes provider-specific.

We had a partial version already -- `SOURCE_ALIASES` and `declines.yaml`
normalise Razorpay's vocabulary at the ingestion boundary -- but the EXECUTION
side called `execution/razorpay.py` directly from the campaign runner. This
closes that half.

The interface is deliberately small. It is the set of things a recovery
sequencer actually needs, not a general payments abstraction:

    capabilities()      what can this provider actually do for us?
    create_recovery()   put a payable artefact in front of a customer
    fetch_recovery()    read one back
    verify_webhook()    is this event genuinely from the provider?

`capabilities()` is the load-bearing one and the reason this is worth having at
all. It is how a provider says "I cannot restrict payment methods on a link" or
"I have no saved mandate for this customer" in a form the sequencer can act on,
rather than the sequencer hard-coding a Razorpay limitation as a universal
truth. Every honesty caveat this project carries -- unenforced rail
restrictions, unexecutable silent retries -- is a capability gap, and this is
where such gaps become data.
"""
from __future__ import annotations

from .base import (Capabilities, Gateway, RecoveryArtefact, WebhookEvent,
                   get_gateway, register)
from .local import LocalGateway
from .razorpay_gateway import RazorpayGateway

register("razorpay", RazorpayGateway)
# A book this project holds itself. Razorpay Subscriptions is not enabled on
# the account and cannot be enabled from here, and the subscription loop needs
# a standing mandate rather than a particular provider. See src/gateways/local.py.
register("local", LocalGateway)

__all__ = ["Capabilities", "Gateway", "LocalGateway", "RecoveryArtefact",
           "WebhookEvent", "RazorpayGateway", "get_gateway", "register"]

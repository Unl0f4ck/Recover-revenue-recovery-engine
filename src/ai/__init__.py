"""The one place this system talks to a language model.

WHAT THE MODEL IS ALLOWED TO DO. Read text a customer wrote, and say what it
thinks the customer meant. That is all.

WHAT IT IS NOT ALLOWED TO DO. Decide anything. It cannot move money, send a
message, skip a guard, set a date, or suppress a contact. Every proposal it
makes goes through the same deterministic validation an operator's typing goes
through -- the promise horizon, the broken-promise ceiling, the future-date
check, the opt-out list. A hallucinated date is refused by `promises.record`
exactly as a typo would be.

That boundary is the whole design, and it is why an LLM is safe here at all.
The engine was fully deterministic before this and still is: nothing below
`src/ai/` is on the path that decides what to do about a case.

WHY IT EARNS ITS PLACE ANYWAY. Two features already built need a human to type
something a machine could read. Promise-to-pay needs an operator to enter a
date from a reply. Opt-out needs someone to notice "STOP" and run a CLI
command. Both are text-understanding problems sitting in the middle of an
otherwise automatic loop, and both are exactly what a language model is for.
"""
from __future__ import annotations

from .provider import (LLMError, LLMUnavailable, Provider, Result, available,
                       get_provider)

__all__ = ["Provider", "Result", "LLMError", "LLMUnavailable",
           "get_provider", "available"]

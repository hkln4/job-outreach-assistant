"""LLM call logging and cost reporting (CLAUDE.md section 8).

Every provider the factory builds is wrapped in LoggedLLMProvider, so each
call writes one LLMCallLog row. Business logic only says what a call is for:

    with llm_context("research", company_id=company.id):
        llm.generate(...)

`llm_context` also works as a decorator (`@llm_context("draft")`), and
`set_llm_ids(company_id=...)` adds ids inside it once they are known. The
context is a contextvar, so nested helpers inherit it and a missing one just
logs purpose "unknown". Logging uses its own session and never raises -
a failed cost row must not fail the call it describes.
"""
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from app.config import get_settings
from app.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger("app.llm.call_log")

_CONTEXT: ContextVar[dict] = ContextVar("llm_call_context", default={})

# USD per million tokens (input, output), first-party list prices. Models not
# listed show tokens only; LLM_PRICES in .env adds or overrides entries.
DEFAULT_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}

_ID_KEYS = ("company_id", "application_id", "profile_item_id")


@contextmanager
def llm_context(purpose: str | None = None, **ids: int | None) -> Iterator[None]:
    """Tag every LLM call inside the block. Inner blocks override outer keys."""
    merged = dict(_CONTEXT.get())
    if purpose:
        merged["purpose"] = purpose
    merged.update({key: value for key, value in ids.items() if key in _ID_KEYS and value is not None})
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def set_llm_ids(**ids: int | None) -> None:
    """Add ids to the current context for the rest of the enclosing
    llm_context block (used as a decorator, the block restores it on exit)."""
    merged = dict(_CONTEXT.get())
    merged.update({key: value for key, value in ids.items() if key in _ID_KEYS and value is not None})
    _CONTEXT.set(merged)


def current_context() -> dict:
    return dict(_CONTEXT.get())


def _write(row: dict) -> None:
    from app.db import SessionLocal
    from app.models import LLMCallLog

    db = SessionLocal()
    try:
        db.add(LLMCallLog(**row))
        db.commit()
    except Exception as exc:  # noqa: BLE001 - logging must never break the call
        db.rollback()
        logger.warning("Could not record LLM call: %s", exc)
    finally:
        db.close()


class LoggedLLMProvider(LLMProvider):
    """Wraps a provider; same interface, plus one LLMCallLog row per call."""

    def __init__(self, inner: LLMProvider, provider_name: str = "", model: str = ""):
        self.inner = inner
        self._provider_name = provider_name
        self._model = model

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048, json_schema=None):
        context = current_context()
        row = {
            "purpose": context.get("purpose", "unknown"),
            "provider": self._provider_name,
            "model": self._model,
            **{key: context.get(key) for key in _ID_KEYS},
        }
        try:
            response: LLMResponse = self.inner.generate(
                prompt,
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                json_schema=json_schema,
            )
        except Exception as exc:
            _write({**row, "error": f"{type(exc).__name__}: {exc}"[:500]})
            raise
        _write(
            {
                **row,
                "model": response.model or self._model,
                "provider": response.provider or self._provider_name,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
        )
        return response


def price_table() -> dict[str, tuple[float, float]]:
    """DEFAULT_PRICES_PER_MTOK plus LLM_PRICES ("model=in/out,model=in/out")."""
    prices = dict(DEFAULT_PRICES_PER_MTOK)
    for entry in (get_settings().llm_prices or "").split(","):
        name, _, value = entry.partition("=")
        price_in, _, price_out = value.partition("/")
        try:
            prices[name.strip()] = (float(price_in), float(price_out))
        except ValueError:
            continue
    return prices


def _price_for(model: str, prices: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    if model in prices:
        return prices[model]
    # Providers sometimes report a dated snapshot ("claude-haiku-4-5-2025...").
    for name, price in sorted(prices.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(name):
            return price
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int, prices: dict | None = None) -> float | None:
    """Estimated cost, or None when the model has no known price."""
    price = _price_for(model, prices if prices is not None else price_table())
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def cost_summary(db) -> dict:
    """Totals for the /costs page: per month, per purpose, per company, per
    application. Costs of models without a price are reported as unpriced
    tokens instead of being counted as zero."""
    from app.models import LLMCallLog

    prices = price_table()
    rows = db.query(LLMCallLog).order_by(LLMCallLog.created_at).all()

    def bucket() -> dict:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "unpriced_tokens": 0, "errors": 0}

    total = bucket()
    by_month: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    by_company: dict[int, dict] = {}
    by_application: dict[int, dict] = {}
    for row in rows:
        cost = cost_usd(row.model, row.input_tokens, row.output_tokens, prices)
        created = row.created_at or datetime.now(timezone.utc)
        targets = [total, by_month.setdefault(created.strftime("%Y-%m"), bucket()), by_purpose.setdefault(row.purpose, bucket())]
        if row.company_id is not None:
            targets.append(by_company.setdefault(row.company_id, bucket()))
        if row.application_id is not None:
            targets.append(by_application.setdefault(row.application_id, bucket()))
        for target in targets:
            target["calls"] += 1
            target["input_tokens"] += row.input_tokens
            target["output_tokens"] += row.output_tokens
            if row.error:
                target["errors"] += 1
            if cost is None:
                target["unpriced_tokens"] += row.input_tokens + row.output_tokens
            else:
                target["cost"] += cost
    return {
        "total": total,
        "by_month": dict(sorted(by_month.items(), reverse=True)),
        "by_purpose": dict(sorted(by_purpose.items(), key=lambda kv: -kv[1]["cost"])),
        "by_company": by_company,
        "by_application": by_application,
    }

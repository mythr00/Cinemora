"""Payment providers (Phase 8; directive §10).

One interface, one implementation. Paddle (Paddle Billing) is
the selected provider — see PHASE8-STAGE2-DESIGN.md §6 for the
evaluation (Stripe is not directly available to a Nigerian
founder, Lemon Squeezy was reported closed to new sellers,
PayPal receiving is restricted; Paddle is a merchant of record,
absorbing global VAT/sales tax, with subscription-native
proration/dunning and signed webhooks).

Design rules:
- The provider ships UNCONFIGURED. Without PADDLE_API_KEY /
  PADDLE_WEBHOOK_SECRET / PADDLE_CLIENT_TOKEN, every entry point
  reports not-configured and checkout refuses with 503 — nothing
  is simulated, ever.
- Our database is the catalogue: checkouts use Paddle ad-hoc
  (non-catalog) prices built from our plan/package rows, so no
  Paddle-side product setup is needed at activation and prices
  can never drift between the two systems.
- Every checkout carries server-set custom_data (user, kind,
  plan/package, interval, expected amount). Webhook processing
  trusts ONLY custom_data + verified signature — never anything
  a browser claims.
- Benefits are granted exclusively by services.billing.webhooks
  from verified provider state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import requests


class ProviderNotConfigured(Exception):
    """Raised when a provider operation needs credentials that
    are not set. The API layer maps it to 503."""


class ProviderError(Exception):
    """A provider API call failed."""


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


class PaddleProvider:
    name = "paddle"

    def __init__(self):
        self.api_key = _env("PADDLE_API_KEY")
        self.webhook_secret = _env("PADDLE_WEBHOOK_SECRET")
        self.client_token = _env("PADDLE_CLIENT_TOKEN")
        environment = (_env("PADDLE_ENVIRONMENT") or "production")
        self.environment = environment
        self.base_url = (
            "https://sandbox-api.paddle.com"
            if environment == "sandbox"
            else "https://api.paddle.com"
        )

    # -- configuration -------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.api_key and self.webhook_secret)

    def public_config(self) -> dict:
        """What the frontend needs to render checkout — the
        client token is a public, checkout-scoped credential by
        design; the API key and webhook secret never leave the
        server."""
        return {
            "provider": self.name,
            "configured": self.is_configured(),
            "client_token": self.client_token,
            "environment": self.environment,
        }

    def _require_configured(self):
        if not self.is_configured():
            raise ProviderNotConfigured(
                "Paddle is not configured on this deployment "
                "(PADDLE_API_KEY / PADDLE_WEBHOOK_SECRET are unset)."
            )

    # -- API ------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None):
        self._require_configured()
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Paddle request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"Paddle {method} {path} -> {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json().get("data", {})

    def create_checkout(
        self,
        *,
        user_email: str,
        custom_data: dict,
        items: list[dict],
        success_url: str | None = None,
    ) -> dict:
        """Create a transaction whose checkout the frontend opens
        with Paddle.js. `items` carry ad-hoc prices built from
        our catalogue rows."""
        payload: dict = {
            "items": items,
            "custom_data": custom_data,
            "customer": {"email": user_email},
        }
        if success_url:
            payload["checkout"] = {"url": success_url}
        data = self._request("POST", "/transactions", payload)
        checkout = data.get("checkout") or {}
        return {
            "provider": self.name,
            "transaction_id": data.get("id"),
            "checkout_url": checkout.get("url"),
            "status": data.get("status"),
        }

    def update_subscription(
        self, subscription_id: str, payload: dict
    ) -> dict:
        return self._request(
            "PATCH", f"/subscriptions/{subscription_id}", payload
        )

    def cancel_subscription(
        self, subscription_id: str, *, effective_from: str = "next_billing_period"
    ) -> dict:
        return self._request(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            {"effective_from": effective_from},
        )

    def create_management_url(self, customer_id: str) -> str | None:
        """A Paddle customer-portal session URL (payment methods,
        invoices, cancellation) — Paddle hosts the surface, we
        never touch card data."""
        data = self._request(
            "POST", f"/customers/{customer_id}/portal-sessions", {}
        )
        urls = data.get("urls") or {}
        general = urls.get("general") or {}
        return general.get("overview")

    # -- webhooks ---------------------------------------------------
    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Paddle-Signature: ts=<unix>;h1=<hex hmac-sha256 of
        f"{ts}:{raw_body}"> with the webhook secret. Timestamp
        tolerance 300s against replay."""
        if not self.webhook_secret:
            return False
        signature = headers.get("paddle-signature") or headers.get(
            "Paddle-Signature"
        )
        if not signature:
            return False
        parts = {}
        for chunk in signature.split(";"):
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                parts[key.strip()] = value.strip()
        ts = parts.get("ts")
        h1 = parts.get("h1")
        if not ts or not h1:
            return False
        try:
            timestamp = int(ts)
        except ValueError:
            return False
        if abs(time.time() - timestamp) > 300:
            return False
        signed = f"{ts}:".encode() + raw_body
        expected = hmac.new(
            self.webhook_secret.encode(), signed, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, h1)

    def parse_event(self, raw_body: bytes) -> dict:
        body = json.loads(raw_body.decode("utf-8"))
        return {
            "event_id": body.get("event_id"),
            "event_type": body.get("event_type"),
            "data": body.get("data") or {},
        }


def get_provider() -> PaddleProvider:
    return PaddleProvider()


def subscription_price_item(plan, interval: str) -> dict:
    """An ad-hoc recurring price item for a plan, built from the
    catalogue row (integer cents)."""
    if interval == "year":
        amount = plan.price_annual_cents
        cycle = {"interval": "year", "frequency": 1}
    else:
        amount = plan.price_monthly_cents
        cycle = {"interval": "month", "frequency": 1}
    return {
        "quantity": 1,
        "price": {
            "description": f"Cinemora {plan.name} ({interval})",
            "name": f"Cinemora {plan.name}",
            "billing_cycle": cycle,
            "trial_period": None,
            "tax_mode": "account_setting",
            "unit_price": {
                "amount": str(amount),
                "currency_code": plan.currency or "USD",
            },
            "custom_data": {"plan_name": plan.name},
        },
    }


def topup_price_item(package) -> dict:
    return {
        "quantity": 1,
        "price": {
            "description": f"Cinemora credits top-up: {package.credits} credits",
            "name": f"Cinemora top-up {package.credits}",
            "tax_mode": "account_setting",
            "unit_price": {
                "amount": str(package.price_cents),
                "currency_code": package.currency or "USD",
            },
            "custom_data": {"package_name": package.name},
        },
    }

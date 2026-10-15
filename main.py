from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
EXPORT_FILE = BASE_DIR / "export.ndjson"

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
UTC_TZ = ZoneInfo("UTC")

API_BASE = "https://exam.sanand.workers.dev/questionData"

EMAIL = "24f1001102@ds.study.iitm.ac.in"

QUIZ_SIGN = (
    "clw75oeT32Y/4vjsRdp7nJPUhe9UMsSFCLWcA7IvMDPAJQ4MqFJ58z"
    "+/EJm7Dvw64BDc/NAIeInt8KXf0nLT9WJRJigoASrBjVhYlDTnpjhgZZ"
    "MnwII25IrhXBxFUC+rUHO5hjoUuenb94Bs7xsOUnS3uGcMJKJcF/UfW4l"
    "/cXK2JM5z0oaq+d/hEz1rLNBtu4oOq2U8J1frIpxSeMJ1bmgaUqWAL"
    "+mVhPl542wgpyOB+bFgDB8KSOt3zC4G8Iaft/QlzSISQMuBv6imDy78oik"
    "HUaAv5jNoDBfLygIkv5MJVs24XSerrEtgGDz1iXPiD+b0tR+b5KgQfBkJ"
    "3MH9SA="
)

DEFAULT_RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.03"),
    "INR": Decimal("0.012"),
}

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "Acme-Ledger-Agent/1.0",
})

LOCK = threading.RLock()


# ============================================================
# HELPERS
# ============================================================

def api_url(path: str | None = None) -> str:
    params = {
        "email": EMAIL,
        "quizSign": QUIZ_SIGN,
    }

    if path:
        params["path"] = path

    return f"{API_BASE}?{urlencode(params)}"


def fetch_json(path: str | None = None) -> Any:
    response = SESSION.get(api_url(path), timeout=30)
    response.raise_for_status()
    return response.json()


def decimal_value(value: Any) -> Decimal:
    try:
        if value is None or isinstance(value, bool):
            return Decimal("0")

        result = Decimal(str(value))

        return result if result.is_finite() else Decimal("0")

    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        text = str(value).strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        result = datetime.fromisoformat(text)

        if result.tzinfo is None:
            result = result.replace(tzinfo=BUSINESS_TZ)

        return result.astimezone(BUSINESS_TZ)

    except (ValueError, TypeError, OverflowError):
        return None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def round_money(value: Decimal) -> Decimal:
    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def extract_month_year(question: str) -> tuple[int | None, int | None]:
    q = question.casefold()

    months = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sept": 9, "sep": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    numeric_match = re.search(
        r"\b(20\d{2})-(0?[1-9]|1[0-2])\b",
        q,
    )

    if numeric_match:
        return (
            int(numeric_match.group(1)),
            int(numeric_match.group(2)),
        )

    for name, number in months.items():
        if re.search(rf"\b{re.escape(name)}\b", q):
            return year, number

    return year, None


def extract_region(question: str) -> str | None:
    q = question.casefold()

    for region in ("North", "South", "East", "West", "Central"):
        if re.search(rf"\b{region.casefold()}\b", q):
            return region

    return None


PRODUCTS = (
    "Rice Cooker",
    "Air Fryer",
    "Blender",
    "Grinder",
    "Juicer",
    "Kettle",
    "Mixer",
    "Toaster",
)


def extract_product(question: str) -> str | None:
    q = question.casefold()

    for product in sorted(PRODUCTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(product.casefold())}\b", q):
            return product

    return None


# ============================================================
# LEDGER DATA
# ============================================================

class Ledger:

    def __init__(self) -> None:
        self.raw_orders: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.rates = dict(DEFAULT_RATES)
        self.last_refresh: str | None = None
        self.load_error: str | None = None

    @staticmethod
    def parse_ndjson(text: str) -> list[dict[str, Any]]:
        records = []

        for line in text.splitlines():
            line = line.strip()

            if not line:
                continue

            try:
                record = json.loads(line)

                if isinstance(record, dict):
                    records.append(record)

            except json.JSONDecodeError:
                continue

        return records

    @staticmethod
    def read_local_export() -> list[dict[str, Any]]:
        if not EXPORT_FILE.exists():
            return []

        with EXPORT_FILE.open("r", encoding="utf-8") as file:
            return Ledger.parse_ndjson(file.read())

    def load_rates(self) -> None:
        rates = dict(DEFAULT_RATES)

        try:
            data = fetch_json("/rates")

            if isinstance(data, dict):
                rate_data = data.get("usd_per_unit", data)

                if isinstance(rate_data, dict):
                    for currency, value in rate_data.items():
                        code = str(currency).upper()
                        rate = decimal_value(value)

                        if rate > 0:
                            rates[code] = rate

        except (
            requests.RequestException,
            ValueError,
            TypeError,
        ) as exc:
            print("Warning: using fallback currency rates:", exc)

        rates["USD"] = Decimal("1")
        self.rates = rates

    def fetch_orders(self) -> list[dict[str, Any]]:
        try:
            response = SESSION.get(
                api_url("/export"),
                timeout=60,
            )
            response.raise_for_status()

            body = response.text.strip()

            if not body:
                raise ValueError("Empty export response")

            try:
                data = response.json()

                if isinstance(data, list):
                    records = [
                        item for item in data
                        if isinstance(item, dict)
                    ]

                elif isinstance(data, dict):
                    records = []

                    for key in ("orders", "data", "records", "results"):
                        if isinstance(data.get(key), list):
                            records = [
                                item for item in data[key]
                                if isinstance(item, dict)
                            ]
                            break

                    if not records and "id" in data:
                        records = [data]

                else:
                    records = []

            except ValueError:
                records = self.parse_ndjson(body)

            if records:
                return records

            raise ValueError("No records found in API response")

        except (
            requests.RequestException,
            ValueError,
            TypeError,
        ) as exc:
            records = self.read_local_export()

            if records:
                print("Using local export:", exc)
                return records

            raise RuntimeError(
                "Unable to load orders from API or local export"
            ) from exc

    @staticmethod
    def deduplicate(
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}

        for record in records:
            order_id = record.get("id")

            if order_id is None:
                continue

            key = str(order_id)
            current = latest.get(key)

            if current is None:
                latest[key] = record
                continue

            old_time = parse_datetime(current.get("updated_at"))
            new_time = parse_datetime(record.get("updated_at"))

            if new_time is None and old_time is None:
                latest[key] = record

            elif new_time is not None and (
                old_time is None or new_time >= old_time
            ):
                latest[key] = record

        return list(latest.values())

    def refresh(self) -> dict[str, Any]:
        with LOCK:
            self.load_rates()
            raw = self.fetch_orders()
            current = self.deduplicate(raw)

            if not current:
                raise RuntimeError("The ledger contains no valid orders")

            self.raw_orders = raw
            self.orders = current
            self.last_refresh = datetime.now(UTC_TZ).isoformat()
            self.load_error = None

            try:
                with EXPORT_FILE.open("w", encoding="utf-8") as file:
                    for record in raw:
                        file.write(
                            json.dumps(record, ensure_ascii=False) + "\n"
                        )
            except OSError as exc:
                print("Warning: could not save export:", exc)

            return {
                "status": "ok",
                "raw_orders": len(raw),
                "orders_loaded": len(current),
                "last_refresh": self.last_refresh,
            }

    def usd_amount(self, order: dict[str, Any]) -> Decimal:
        currency = str(order.get("currency", "USD")).upper()
        rate = self.rates.get(currency)

        if rate is None:
            raise ValueError(f"Missing exchange rate for {currency}")

        return decimal_value(order.get("amount")) * rate

    def filter_orders(
        self,
        *,
        status: str | None = None,
        region: str | None = None,
        product: str | None = None,
        customer: str | None = None,
        year: int | None = None,
        month: int | None = None,
        date_field: str = "created_at",
        raw: bool = False,
    ) -> list[dict[str, Any]]:

        source = self.raw_orders if raw else self.orders
        result = []

        for order in source:

            if status and normalize_text(order.get("status")) != status:
                continue

            if region and normalize_text(order.get("region")) != normalize_text(region):
                continue

            if product and normalize_text(order.get("product")) != normalize_text(product):
                continue

            if customer and normalize_text(order.get("customer")) != normalize_text(customer):
                continue

            if year is not None or month is not None:
                dt = parse_datetime(order.get(date_field))

                if dt is None:
                    continue

                if year is not None and dt.year != year:
                    continue

                if month is not None and dt.month != month:
                    continue

            result.append(order)

        return result


ledger = Ledger()


# ============================================================
# QUESTION ANSWERING
# ============================================================

def answer_question(question: str) -> Any:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Question cannot be empty")

    q = question.casefold()

    if not ledger.orders:
        ledger.refresh()

    year, month = extract_month_year(question)
    region = extract_region(question)
    product = extract_product(question)

    if re.search(r"\brefund(?:ed|s)?\b", q):
        status = "refunded"
    elif re.search(r"\bvoid(?:ed)?\b", q):
        status = "void"
    else:
        status = "paid"

    orders = ledger.filter_orders(
        status=status,
        region=region,
        product=product,
        year=year,
        month=month,
        date_field="created_at",
    )

    # Count distinct customers, not orders.
    if "customer" in q and any(
        term in q
        for term in (
            "how many",
            "different",
            "distinct",
            "unique",
            "number of",
            "count",
        )
    ):
        return len({
            normalize_text(order.get("customer"))
            for order in orders
            if normalize_text(order.get("customer"))
        })

    # Count distinct products.
    if "product" in q and any(
        term in q
        for term in (
            "how many different",
            "how many distinct",
            "distinct products",
            "unique products",
        )
    ):
        return len({
            normalize_text(order.get("product"))
            for order in orders
            if normalize_text(order.get("product"))
        })

    # Count orders.
    if any(
        phrase in q
        for phrase in (
            "how many orders",
            "number of orders",
            "count of orders",
            "count orders",
            "how many paid",
            "how many refunded",
            "how many void",
        )
    ):
        return len(orders)

    # Sum quantities.
    if any(
        phrase in q
        for phrase in (
            "total quantity",
            "total units",
            "units sold",
            "how many units",
            "quantity sold",
            "total items",
        )
    ):
        return sum(
            int(decimal_value(order.get("qty")))
            for order in orders
        )

    # Average order value in USD.
    if "average" in q or "mean" in q:
        if any(word in q for word in ("revenue", "order value", "amount")):
            if not orders:
                return 0.0

            total = sum(
                (ledger.usd_amount(order) for order in orders),
                Decimal("0"),
            )

            return float(
                (total / Decimal(len(orders))).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

    # Revenue in USD.
    if any(
        word in q
        for word in (
            "revenue",
            "sales",
            "earned",
            "earning",
            "income",
            "turnover",
            "total amount",
        )
    ):
        total = sum(
            (ledger.usd_amount(order) for order in orders),
            Decimal("0"),
        )

        return float(
            total.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

    # Highest- or lowest-revenue product.
    if any(
        phrase in q
        for phrase in (
            "which product",
            "top product",
            "most revenue",
            "highest revenue",
            "least revenue",
            "lowest revenue",
        )
    ):
        totals: dict[str, Decimal] = defaultdict(
            lambda: Decimal("0")
        )

        for order in orders:
            name = str(order.get("product", "")).strip()

            if name:
                totals[name] += ledger.usd_amount(order)

        if not totals:
            return None

        reverse = not any(
            phrase in q
            for phrase in ("least revenue", "lowest revenue")
        )

        ranked = sorted(
            totals.items(),
            key=lambda item: (
                item[1],
                normalize_text(item[0]),
            ),
            reverse=reverse,
        )

        return ranked[0][0]

    # Most/fewest orders by region.
    if "most orders" in q or "fewest orders" in q:
        counts: dict[str, int] = defaultdict(int)

        for order in orders:
            name = str(order.get("region", "")).strip()

            if name:
                counts[name] += 1

        if not counts:
            return None

        reverse = "most orders" in q

        return sorted(
            counts.items(),
            key=lambda item: (
                item[1],
                normalize_text(item[0]),
            ),
            reverse=reverse,
        )[0][0]

    raise ValueError(
        "Question not recognised by the rule-based answer engine."
    )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Acme Appliances Ledger Agent",
    version="1.0.0",
)


class AnswerRequest(BaseModel):
    question: str = Field(..., min_length=1)


@app.on_event("startup")
def startup_event() -> None:
    try:
        result = ledger.refresh()
        print("Ledger loaded:", result)

    except Exception as exc:
        ledger.load_error = str(exc)
        print("Initial load failed:", exc)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Acme Appliances Ledger Agent",
        "status": "ok",
        "endpoints": ["/health", "/refresh", "/answer"],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if ledger.orders else "not_ready",
        "orders_loaded": len(ledger.orders),
        "raw_orders": len(ledger.raw_orders),
        "last_refresh": ledger.last_refresh,
        "load_error": ledger.load_error,
    }


@app.post("/refresh")
def refresh() -> dict[str, Any]:
    try:
        return ledger.refresh()

    except Exception as exc:
        ledger.load_error = str(exc)

        raise HTTPException(
            status_code=502,
            detail=f"Refresh failed: {exc}",
        ) from exc


@app.post("/answer")
def answer(request: AnswerRequest) -> dict[str, Any]:
    try:
        return {
            "answer": answer_question(request.question),
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Answering failed: {exc}",
        ) from exc


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
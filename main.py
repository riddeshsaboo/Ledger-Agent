import os
import re
import json
import calendar
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# --------------------------------------------------
# Configuration
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMAIL = "24f1001102@ds.study.iitm.ac.in"

DATA_URL = (
    "https://exam.sanand.workers.dev/questionData"
    "?email=24f1001102%40ds.study.iitm.ac.in"
    "&quizSign=clw75oeT32Y%2F4vjsRdp7nJPUhe9UMsSFCLWcA7IvMDPAJQ4MqFJ58z%2B%2FEJm7Dvw64BDc%2FNAIeInt8KXf0nLT9WJRJigoASrBjVhYlDTnpjhgZZMnwII25IrhXBxFUC%2BrUHO5hjoUuenb94Bs7xsOUnS3uGcMJKJcF%2FUfW4l%2FcXK2JM5z0oaq%2Bd%2FhEz1rLNBtu4oOq2U8J1frIpxSeMJ1bmgaUqWAL%2BmVhPl542wgpyOB%2BbFgDB8KSOt3zC4G8Iaft%2FQlzSISQMuBv6imDy78oikHUaAv5jNoDBfLygIkv5MJVs24XSerrEtgGDz1iXPiD%2Bb0tR%2Bx5KgQfBkJ3MH9SA%3D%3D"
)

QUESTION_ID = "q-ledger-agent-server"
TIMEZONE = ZoneInfo("Asia/Kolkata")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Acme-Ledger-Agent/1.0"})

ORDERS = []
RATES = {}
LAST_REFRESH = None


# --------------------------------------------------
# Load data
# --------------------------------------------------

def api_url(path):
    return (
        DATA_URL
        + "&questionId="
        + QUESTION_ID
        + "&path="
        + requests.utils.quote(path, safe="/")
    )


def load_data():
    """Load the ledger export and conversion rates."""

    global ORDERS, RATES, LAST_REFRESH

    logger.info("Loading order ledger")

    # Load currency rates.
    rate_response = SESSION.get(api_url("/rates"), timeout=10)
    rate_response.raise_for_status()

    rate_data = rate_response.json()
    RATES = {
        currency: Decimal(str(rate))
        for currency, rate in rate_data["usd_per_unit"].items()
    }

    # Load the complete NDJSON export.
    export_response = SESSION.get(api_url("/export"), timeout=20)
    export_response.raise_for_status()

    records = []

    for line in export_response.text.splitlines():
        line = line.strip()

        if not line:
            continue

        records.append(json.loads(line))

    # Keep the latest version of every order.
    latest = {}

    for row in records:
        order_id = row["id"]

        updated = parse_datetime(row["updated_at"])

        if order_id not in latest:
            latest[order_id] = row
            continue

        existing_updated = parse_datetime(
            latest[order_id]["updated_at"]
        )

        if updated > existing_updated:
            latest[order_id] = row

    ORDERS = list(latest.values())
    LAST_REFRESH = datetime.now(timezone.utc)

    logger.info(
        "Loaded %s records; %s unique orders",
        len(records),
        len(ORDERS),
    )


def parse_datetime(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)

    return dt.astimezone(TIMEZONE)


@asynccontextmanager
async def lifespan(app):
    load_data()
    yield


app = FastAPI(
    title="Acme Appliances Ledger Agent",
    lifespan=lifespan,
)


# --------------------------------------------------
# Request model
# --------------------------------------------------

class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------
# Money and filtering helpers
# --------------------------------------------------

def usd_amount(order):
    currency = order["currency"]

    if currency not in RATES:
        raise ValueError(f"Unknown currency: {currency}")

    return (
        Decimal(str(order["amount"])) * RATES[currency]
    )


def money(value):
    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def extract_filters(question):
    q = normalize(question)

    filters = {}

    # Regions.
    regions = [
        "North",
        "South",
        "East",
        "West",
        "Central",
    ]

    for region in regions:
        if re.search(
            r"\b" + re.escape(region.lower()) + r"\b",
            q,
        ):
            filters["region"] = region
            break

    # Product names.
    products = sorted(
        {row["product"] for row in ORDERS},
        key=len,
        reverse=True,
    )

    for product in products:
        if product.lower() in q:
            filters["product"] = product
            break

    # Customer identifiers.
    customer_match = re.search(
        r"\bc\d{4}\b",
        q,
        re.IGNORECASE,
    )

    if customer_match:
        filters["customer"] = customer_match.group().upper()

    # Currency filters.
    currency_match = re.search(
        r"\b(USD|EUR|INR)\b",
        question,
        re.IGNORECASE,
    )

    if currency_match:
        filters["currency"] = currency_match.group().upper()

    # Month and year.
    months = {
        name.lower(): number
        for number, name in enumerate(calendar.month_name)
        if name
    }

    for month_name, month_number in months.items():
        if re.search(r"\b" + month_name + r"\b", q):
            filters["month"] = month_number
            break

    year_match = re.search(r"\b(20\d{2})\b", q)

    if year_match:
        filters["year"] = int(year_match.group())

    # Explicit date range.
    date_match = re.search(
        r"\bbetween\s+"
        r"(\d{4}-\d{2}-\d{2})"
        r"\s+and\s+"
        r"(\d{4}-\d{2}-\d{2})\b",
        q,
    )

    if date_match:
        filters["start_date"] = datetime.fromisoformat(
            date_match.group(1)
        ).date()

        filters["end_date"] = datetime.fromisoformat(
            date_match.group(2)
        ).date()

    return filters


def filter_orders(orders, question, statuses=None):
    filters = extract_filters(question)
    q = normalize(question)

    # Determine the date field.
    date_field = "created_at"

    if any(
        word in q
        for word in [
            "updated",
            "last updated",
            "status changed",
        ]
    ):
        date_field = "updated_at"

    selected = []

    for order in orders:
        if statuses and order["status"] not in statuses:
            continue

        if "region" in filters:
            if order["region"].lower() != filters["region"].lower():
                continue

        if "product" in filters:
            if order["product"].lower() != filters["product"].lower():
                continue

        if "customer" in filters:
            if order["customer"].upper() != filters["customer"]:
                continue

        if "currency" in filters:
            if order["currency"].upper() != filters["currency"]:
                continue

        dt = parse_datetime(order[date_field])
        order_date = dt.date()

        if "year" in filters and dt.year != filters["year"]:
            continue

        if "month" in filters and dt.month != filters["month"]:
            continue

        if "start_date" in filters:
            if order_date < filters["start_date"]:
                continue

        if "end_date" in filters:
            if order_date > filters["end_date"]:
                continue

        selected.append(order)

    return selected


# --------------------------------------------------
# Question interpretation
# --------------------------------------------------

def answer_question(question):
    q = normalize(question)

    if not ORDERS:
        raise ValueError("The order ledger is empty")

    # Status-specific questions.
    asks_refunds = any(
        word in q
        for word in [
            "refund",
            "refunded",
            "refunds",
        ]
    )

    asks_void = any(
        word in q
        for word in [
            "void",
            "voided",
            "cancelled",
            "canceled",
        ]
    )

    if asks_refunds:
        statuses = {"refunded"}
    elif asks_void:
        statuses = {"void"}
    else:
        statuses = {"paid"}

    selected = filter_orders(
        ORDERS,
        question,
        statuses=statuses,
    )

    if not selected:
        if any(
            word in q
            for word in [
                "product",
                "customer",
                "which",
                "highest",
                "lowest",
                "most",
                "least",
            ]
        ):
            return ""

        return 0

    # Count orders.
    asks_count = any(
        phrase in q
        for phrase in [
            "how many orders",
            "number of orders",
            "count of orders",
            "order count",
            "how many refunds",
            "number of refunds",
            "count of refunds",
            "how many customers",
            "number of customers",
            "unique customers",
        ]
    )

    if asks_count:
        if "customer" in q:
            return len({row["customer"] for row in selected})

        return len(selected)

    # Quantity questions.
    if any(
        phrase in q
        for phrase in [
            "units sold",
            "total units",
            "quantity sold",
            "total quantity",
            "how many units",
            "number of units",
        ]
    ):
        return sum(int(row["qty"]) for row in selected)

    # Ranking questions.
    asks_ranking = any(
        word in q
        for word in [
            "highest",
            "lowest",
            "most",
            "least",
            "top",
            "best",
            "largest",
            "smallest",
        ]
    )

    if asks_ranking:
        if "customer" in q:
            group_field = "customer"
        elif "region" in q:
            group_field = "region"
        else:
            group_field = "product"

        totals = defaultdict(Decimal)

        for row in selected:
            totals[row[group_field]] += usd_amount(row)

        if not totals:
            return ""

        if any(
            word in q
            for word in [
                "lowest",
                "least",
                "smallest",
            ]
        ):
            result = min(totals, key=totals.get)
        else:
            result = max(totals, key=totals.get)

        return result

    # Product/customer/region listing questions.
    if any(
        phrase in q
        for phrase in [
            "which product",
            "what product",
            "which customer",
            "what customer",
            "which region",
            "what region",
        ]
    ) and not asks_ranking:
        if "customer" in q:
            field = "customer"
        elif "region" in q:
            field = "region"
        else:
            field = "product"

        values = sorted({row[field] for row in selected})

        return values[0] if len(values) == 1 else values

    # Financial totals.
    total = sum(
        (usd_amount(row) for row in selected),
        Decimal("0"),
    )

    return float(money(total))


# --------------------------------------------------
# HTTP endpoint
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "service": "Acme Appliances Ledger Agent",
        "status": "ready",
        "endpoint": "/answer",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "orders_loaded": len(ORDERS),
        "last_refresh": (
            LAST_REFRESH.isoformat()
            if LAST_REFRESH
            else None
        ),
    }


@app.post("/answer")
def answer(request: QuestionRequest):
    question = request.question.strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail="Question cannot be empty",
        )

    try:
        result = answer_question(question)

        return {"answer": result}

    except Exception:
        logger.exception("Failed to answer question")

        raise HTTPException(
            status_code=500,
            detail="Unable to answer this question",
        )
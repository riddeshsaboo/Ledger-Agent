import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ============================================================
# Configuration
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
EXPORT_FILE = BASE_DIR / "export.ndjson"

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

DATA_URL = (
    "https://exam.sanand.workers.dev/questionData"
    "?email=24f1001102%40ds.study.iitm.ac.in"
    "&quizSign=clw75oeT32Y%2F4vjsRdp7nJPUhe9UMsSFCLWcA7IvMDPAJQ4MqFJ58z%2B%2FEJm7Dvw64BDc%2FNAIeInt8KXf0nLT9WJRJigoASrBjVhYlDTnpjhgZZMnwII25IrhXBxFUC%2BrUHO5hjoUuenb94Bs7xsOUnS3uGcMJKJcF%2FUfW4l%2FcXK2JM5z0oaq%2Bd%2FhEz1rLNBtu4oOq2U8J1frIpxSeMJ1bmgaUqWAL%2BmVhPl542wgpyOB%2BbFgDB8KSOt3zC4G8Iaft%2FQlzSISQMuBv6imDy78oikHUaAv5jNoDBfLygIkv5MJVs24XSerrEtgGDz1iXPiD%2Bb0tR%2Bx5KgQfBkJ3MH9SA%3D%3D"
)

QUESTION_ID = "q-ledger-agent-server"

# ============================================================
# API helpers
# ============================================================

def api_url(path: str) -> str:
    path = "/" + path.strip("/")
    return (
        DATA_URL
        + "&questionId="
        + QUESTION_ID
        + "&path="
        + requests.utils.quote(path, safe="/")
    )


def fetch_json(path: str):
    response = requests.get(api_url(path), timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_export():
    response = requests.get(api_url("/export"), timeout=120)
    response.raise_for_status()

    rows = []

    for line in response.text.splitlines():
        line = line.strip()

        if line:
            rows.append(json.loads(line))

    return rows


# ============================================================
# Date and numeric helpers
# ============================================================

def parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, including timestamps ending in Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def business_datetime(value: str) -> datetime:
    """Convert a timestamp to the Asia/Kolkata business timezone."""
    dt = parse_datetime(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BUSINESS_TZ)

    return dt.astimezone(BUSINESS_TZ)


def decimal_value(value) -> Decimal:
    return Decimal(str(value))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


# ============================================================
# Ledger service
# ============================================================

class Ledger:
    def __init__(self):
        self.rows = []
        self.orders = []
        self.rates = {}
        self.last_refresh = None

    def load(self):
        """Load exchange rates and the latest version of every order."""
        logger.info("Loading ledger data")

        rates_data = fetch_json("/rates")
        raw_rates = rates_data.get("usd_per_unit", {})

        self.rates = {
            currency.upper(): decimal_value(rate)
            for currency, rate in raw_rates.items()
        }

        raw_rows = fetch_export()

        latest_by_id = {}

        for row in raw_rows:
            order_id = row.get("id")

            if not order_id:
                continue

            updated_at = parse_datetime(row["updated_at"])
            previous = latest_by_id.get(order_id)

            if (
                previous is None
                or updated_at > parse_datetime(previous["updated_at"])
            ):
                latest_by_id[order_id] = row

        self.rows = raw_rows
        self.orders = list(latest_by_id.values())
        self.last_refresh = datetime.now(BUSINESS_TZ).isoformat()

        logger.info(
            "Loaded %d raw rows and %d current orders",
            len(self.rows),
            len(self.orders),
        )

    def ensure_loaded(self):
        if not self.orders or not self.rates:
            self.load()

    def revenue_usd(self, order) -> Decimal:
        currency = str(order["currency"]).upper()

        if currency not in self.rates:
            raise ValueError(f"Unknown currency: {currency}")

        return decimal_value(order["amount"]) * self.rates[currency]

    def filter_orders(
        self,
        *,
        year=None,
        month=None,
        region=None,
        product=None,
        customer=None,
        status="paid",
        date_field="created_at",
    ):
        """
        Filter current orders.

        Revenue calculations use current order versions, paid status,
        and business-local timestamps.
        """
        self.ensure_loaded()

        result = []

        for order in self.orders:
            if status is not None:
                if str(order.get("status", "")).lower() != status.lower():
                    continue

            if region is not None:
                if normalize_text(str(order.get("region", ""))) != normalize_text(region):
                    continue

            if product is not None:
                if normalize_text(str(order.get("product", ""))) != normalize_text(product):
                    continue

            if customer is not None:
                if normalize_text(str(order.get("customer", ""))) != normalize_text(customer):
                    continue

            dt = business_datetime(order[date_field])

            if year is not None and dt.year != year:
                continue

            if month is not None and dt.month != month:
                continue

            result.append(order)

        return result

    def total_revenue(self, orders) -> Decimal:
        return sum(
            (self.revenue_usd(order) for order in orders),
            Decimal("0"),
        )

    def revenue_by(self, orders, field):
        totals = defaultdict(lambda: Decimal("0"))

        for order in orders:
            key = order.get(field)

            if key is not None:
                totals[str(key)] += self.revenue_usd(order)

        return dict(totals)

    def count_by(self, orders, field):
        counts = defaultdict(int)

        for order in orders:
            key = order.get(field)

            if key is not None:
                counts[str(key)] += 1

        return dict(counts)


ledger = Ledger()

# ============================================================
# Question parsing
# ============================================================

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def extract_period(question):
    q = normalize_text(question)

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    month = None

    for month_name, month_number in MONTHS.items():
        if re.search(r"\b" + re.escape(month_name) + r"\b", q):
            month = month_number
            break

    return year, month


def extract_region(question):
    q = normalize_text(question)

    regions = ["central", "north", "south", "east", "west"]

    for region in regions:
        if re.search(r"\b" + region + r"\b", q):
            return region.title()

    return None


def extract_product(question):
    q = normalize_text(question)

    products = [
        "Rice Cooker",
        "Air Fryer",
        "Kettle",
        "Mixer",
        "Toaster",
        "Grinder",
        "Blender",
        "Juicer",
    ]

    for product in products:
        if normalize_text(product) in q:
            return product

    return None


def extract_customer(question):
    match = re.search(r"\bC\d{4}\b", question, re.IGNORECASE)
    return match.group(0).upper() if match else None


def money(value):
    return value.quantize(Decimal("0.01"))


# ============================================================
# Answer generation
# ============================================================

def answer_question(question: str):
    q = normalize_text(question)

    ledger.ensure_loaded()

    year, month = extract_period(question)
    region = extract_region(question)
    product = extract_product(question)
    customer = extract_customer(question)

    # The service notes define revenue as paid orders only.
    orders = ledger.filter_orders(
        year=year,
        month=month,
        region=region,
        product=product,
        customer=customer,
        status="paid",
        date_field="created_at",
    )

    # --------------------------------------------------------
    # Total revenue
    # --------------------------------------------------------

    if "revenue" in q or "sales" in q:
        if any(word in q for word in ["which product", "top product", "most", "highest"]):
            totals = ledger.revenue_by(orders, "product")

            if not totals:
                return "No matching paid orders found."

            winner = max(totals, key=totals.get)
            return winner

        if any(word in q for word in ["which region", "top region"]):
            totals = ledger.revenue_by(orders, "region")

            if not totals:
                return "No matching paid orders found."

            return max(totals, key=totals.get)

        if any(word in q for word in ["which customer", "top customer"]):
            totals = ledger.revenue_by(orders, "customer")

            if not totals:
                return "No matching paid orders found."

            return max(totals, key=totals.get)

        return str(money(ledger.total_revenue(orders)))

    # --------------------------------------------------------
    # Order counts
    # --------------------------------------------------------

    if any(word in q for word in ["how many", "number of orders", "count of orders"]):
        return str(len(orders))

    # --------------------------------------------------------
    # Quantity questions
    # --------------------------------------------------------

    if "quantity" in q or "units sold" in q or "how many units" in q:
        total_qty = sum(int(order.get("qty", 0)) for order in orders)
        return str(total_qty)

    # --------------------------------------------------------
    # Average order value
    # --------------------------------------------------------

    if "average order value" in q or "average revenue per order" in q:
        if not orders:
            return "0.00"

        average = ledger.total_revenue(orders) / Decimal(len(orders))
        return str(money(average))

    # --------------------------------------------------------
    # Status counts
    # --------------------------------------------------------

    if "refunded" in q or "void" in q:
        requested_status = (
            "refunded" if "refunded" in q else "void"
        )

        status_orders = ledger.filter_orders(
            year=year,
            month=month,
            region=region,
            product=product,
            customer=customer,
            status=requested_status,
            date_field="created_at",
        )

        return str(len(status_orders))

    # --------------------------------------------------------
    # Most common product
    # --------------------------------------------------------

    if any(word in q for word in ["most ordered product", "most popular product"]):
        counts = ledger.count_by(orders, "product")

        if not counts:
            return "No matching orders found."

        return max(counts, key=counts.get)

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    raise ValueError(
        "I could not determine how to answer this question: " + question
    )


# ============================================================
# FastAPI application
# ============================================================

app = FastAPI(
    title="Acme Appliances Order Ledger",
    version="1.0.0",
)


class QuestionRequest(BaseModel):
    question: str


@app.on_event("startup")
def startup_event():
    try:
        ledger.load()
    except Exception:
        logger.exception("Initial ledger loading failed")


@app.get("/")
def root():
    return {
        "service": "Acme Appliances order ledger",
        "status": "running",
    }


@app.get("/health")
def health():
    return {
        "status": "ok" if ledger.orders else "not_loaded",
        "orders_loaded": len(ledger.orders),
        "last_refresh": ledger.last_refresh,
    }


@app.post("/refresh")
def refresh():
    try:
        ledger.load()

        return {
            "status": "ok",
            "raw_rows": len(ledger.rows),
            "orders_loaded": len(ledger.orders),
            "last_refresh": ledger.last_refresh,
        }

    except Exception:
        logger.exception("Ledger refresh failed")
        raise HTTPException(
            status_code=500,
            detail="Unable to refresh ledger",
        )


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

    except ValueError as exc:
        logger.warning("Question parsing failed: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except Exception:
        logger.exception("Failed to answer question")

        raise HTTPException(
            status_code=500,
            detail="Unable to answer this question",
        )
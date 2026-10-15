import json
import re
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = (
    "https://exam.sanand.workers.dev/questionData"
    "?email=24f3001311%40ds.study.iitm.ac.in"
    "&quizSign=qaDMHhDfPMnBJcU%2BFeBuxXtwY7xWzn3xG%2F3dOYEufQcegy95Ub5yeb2%2FcMsgy1taz0jK9uOI9%2FLP49TGZHoOvo04ZWv7CpLwoYiKXSq4h1nBHTQfKO1%2BtDiuqYcmj%2B6wrkOl80aCLFD9B6R7n2PScSGSOeOMsUWcrlzyq0qZ0gSLHMT%2BUpendMsv3NCJy%2FATctMHijhyYtfQpmXa4tQicu9XC3%2FjXGTt4G1OYjw6VOEZCdv3a948waETDJTLl164m6ZyTKd2KU16ha1kCflXNERb4Lpfx66566JYAKaXe0zSgFP0xS9Xl%2FP1ENC0C8qiB1vagsgvfENaUfLkKf0m7w%3D%3D"
    "&questionId=q-ledger-agent-server"
)

RATES_URL = BASE_URL + "&path=%2Frates"
EXPORT_URL = BASE_URL + "&path=%2Fexport"

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}

MONTH_MAP = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

PRODUCTS = [
    "Air Fryer",
    "Rice Cooker",
    "Juicer",
    "Toaster",
    "Mixer",
    "Kettle",
    "Blender",
    "Grinder",
]

REGIONS = ["North", "South", "East", "West", "Central"]

app = FastAPI(title="Acme Appliances Ledger Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db_conn = sqlite3.connect(":memory:", check_same_thread=False)
db_conn.row_factory = sqlite3.Row
db_lock = threading.RLock()

database_ready = False
orders_loaded = 0
last_refresh = None
usd_rates = {}


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_database():
    global database_ready, orders_loaded, last_refresh, usd_rates

    print("Fetching currency rates...")

    with httpx.Client(timeout=45.0, follow_redirects=True) as client:
        rates_response = client.get(RATES_URL, headers=HEADERS)
        rates_response.raise_for_status()

        rates_data = rates_response.json()
        usd_rates = rates_data["usd_per_unit"]

        print("Currency rates loaded:", usd_rates)

        print("Fetching order ledger...")

        export_response = client.get(EXPORT_URL, headers=HEADERS)
        export_response.raise_for_status()

        raw_text = export_response.text.strip()

    if not raw_text:
        raise RuntimeError("The order export is empty.")

    rows = [
        json.loads(line)
        for line in raw_text.splitlines()
        if line.strip()
    ]

    if not rows:
        raise RuntimeError("No order records were returned.")

    df = pd.DataFrame(rows)

    required_columns = {
        "id",
        "customer",
        "region",
        "product",
        "qty",
        "amount",
        "currency",
        "status",
        "created_at",
        "updated_at",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Order export is missing required columns: {sorted(missing)}"
        )

    print(f"Raw records downloaded: {len(df)}")

    # Parse timestamps as UTC, regardless of their original offsets.
    df["created_at_utc"] = pd.to_datetime(
        df["created_at"],
        format="ISO8601",
        utc=True,
        errors="coerce",
    )

    df["updated_at_utc"] = pd.to_datetime(
        df["updated_at"],
        format="ISO8601",
        utc=True,
        errors="coerce",
    )

    if df["created_at_utc"].isna().any():
        raise RuntimeError("Some orders have invalid creation timestamps.")

    if df["updated_at_utc"].isna().any():
        raise RuntimeError("Some orders have invalid update timestamps.")

    # Latest updated record wins for each order ID.
    # Stable sorting makes ties deterministic.
    df = (
        df.sort_values("updated_at_utc", kind="mergesort")
        .drop_duplicates(subset=["id"], keep="last")
        .copy()
    )

    # Normalize text fields.
    for column in ["status", "currency", "product", "region", "customer"]:
        df[column] = df[column].astype(str).str.strip()

    df["status"] = df["status"].str.lower()
    df["currency"] = df["currency"].str.upper()

    # Numeric validation.
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    if df["qty"].isna().any() or df["amount"].isna().any():
        raise RuntimeError("Some orders have invalid quantities or amounts.")

    # Convert business dates to Asia/Kolkata.
    local_created = df["created_at_utc"].dt.tz_convert(BUSINESS_TZ)

    df["created_date"] = local_created.dt.strftime("%Y-%m-%d")
    df["created_month"] = local_created.dt.strftime("%Y-%m")
    df["created_year"] = local_created.dt.year.astype(int)

    # Convert all amounts into USD using the supplied rates.
    df["rate_to_usd"] = df["currency"].map(usd_rates)

    if df["rate_to_usd"].isna().any():
        unknown = sorted(
            df.loc[df["rate_to_usd"].isna(), "currency"].unique().tolist()
        )
        raise RuntimeError(f"Missing USD conversion rates for: {unknown}")

    df["amount_usd"] = df["amount"] * df["rate_to_usd"]

    # Replace the database contents atomically.
    sql_df = df.drop(
        columns=["created_at_utc", "updated_at_utc"]
    ).copy()

    with db_lock:
        sql_df.to_sql(
            "orders",
            db_conn,
            index=False,
            if_exists="replace",
        )

        db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status "
            "ON orders(status)"
        )

        db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_month "
            "ON orders(created_month)"
        )

        db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_product "
            "ON orders(product)"
        )

        db_conn.commit()

    orders_loaded = len(sql_df)
    last_refresh = datetime.now(BUSINESS_TZ).isoformat()
    database_ready = True

    print(f"Verified current orders: {orders_loaded}")
    print("SQLite database initialized successfully.")


# ============================================================
# QUESTION PARSING
# ============================================================

def extract_month_and_year(question):
    q = question.lower()

    month = None

    for month_name, month_number in MONTH_MAP.items():
        if re.search(rf"\b{month_name}\b", q):
            month = month_number
            break

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    if month and year is None:
        year = 2026

    return month, year


def build_filters(question):
    q = question.lower()
    conditions = []
    params = []

    # Status filtering.
    # Questions explicitly about refunded or voided orders use those
    # statuses. Otherwise, financial/order analyses default to paid.
    if re.search(r"\b(refunded|refunds|refund)\b", q):
        conditions.append("status = ?")
        params.append("refunded")

    elif re.search(r"\b(void|voided|cancelled|canceled)\b", q):
        conditions.append("status = ?")
        params.append("void")

    else:
        conditions.append("status = ?")
        params.append("paid")

    # Region filtering.
    for region in REGIONS:
        if re.search(rf"\b{re.escape(region.lower())}\b", q):
            conditions.append("region = ?")
            params.append(region)
            break

    # Product filtering.
    for product in sorted(PRODUCTS, key=len, reverse=True):
        pattern = rf"\b{re.escape(product.lower())}(?:s)?\b"

        if re.search(pattern, q):
            conditions.append("product = ?")
            params.append(product)
            break

    # Customer filtering.
    customer_match = re.search(r"\b(c\d{4})\b", q, re.IGNORECASE)

    if customer_match:
        conditions.append("customer = ?")
        params.append(customer_match.group(1).upper())

    # Date filtering.
    month, year = extract_month_and_year(question)

    if month and year:
        conditions.append("created_month = ?")
        params.append(f"{year}-{month}")

    elif year and re.search(r"\b(year|annual|yearly)\b", q):
        conditions.append("created_year = ?")
        params.append(year)

    # Specific date, e.g. 2026-03-15.
    date_match = re.search(
        r"\b(20\d{2}-\d{2}-\d{2})\b",
        q,
    )

    if date_match:
        conditions.append("created_date = ?")
        params.append(date_match.group(1))

    return conditions, params


# ============================================================
# SAFE SQL EXECUTION
# ============================================================

def execute_scalar(sql, params):
    with db_lock:
        cursor = db_conn.execute(sql, params)
        row = cursor.fetchone()

    if row is None:
        return 0

    return row[0] if row[0] is not None else 0


def execute_query(sql, params):
    with db_lock:
        cursor = db_conn.execute(sql, params)
        return cursor.fetchall()


def parse_question_to_sql(question):
    q = question.lower().strip()

    if not q:
        raise ValueError("Question cannot be empty.")

    conditions, params = build_filters(question)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # --------------------------------------------------------
    # Highest/lowest product by revenue or quantity.
    # Check rankings before generic revenue queries.
    # --------------------------------------------------------

    product_ranking = (
        "which product",
        "what product",
        "top-selling product",
        "top selling product",
        "best-selling product",
        "best selling product",
        "product earned the most",
        "product generated the most",
        "highest revenue product",
        "lowest revenue product",
        "most revenue",
        "least revenue",
    )

    if any(phrase in q for phrase in product_ranking):
        quantity_words = ("quantity", "units sold", "number of units")
        revenue_words = (
            "revenue",
            "earned",
            "earnings",
            "sales value",
            "in usd",
            "amount",
        )

        if any(word in q for word in quantity_words):
            metric = "SUM(qty)"
        elif any(word in q for word in revenue_words):
            metric = "SUM(amount_usd)"
        else:
            # "Top-selling" without another metric means units sold.
            metric = "SUM(qty)"

        direction = (
            "ASC"
            if any(word in q for word in ("lowest", "least", "minimum"))
            else "DESC"
        )

        sql = f"""
            SELECT product
            FROM orders
            WHERE {where_clause}
            GROUP BY product
            ORDER BY {metric} {direction}, product ASC
            LIMIT 1
        """

        return sql, params, "string"

    # --------------------------------------------------------
    # Highest/lowest customer by spending.
    # --------------------------------------------------------

    customer_ranking = (
        "which customer",
        "what customer",
        "top customer",
        "customer spent the most",
        "customer spending the most",
        "highest spending customer",
        "biggest spender",
    )

    if any(phrase in q for phrase in customer_ranking):
        sql = f"""
            SELECT customer
            FROM orders
            WHERE {where_clause}
            GROUP BY customer
            ORDER BY SUM(amount_usd) DESC, customer ASC
            LIMIT 1
        """

        return sql, params, "string"

    # --------------------------------------------------------
    # Highest/lowest region by revenue.
    # --------------------------------------------------------

    region_ranking = (
        "which region",
        "what region",
        "top region",
        "region earned the most",
        "region generated the most",
        "highest revenue region",
    )

    if any(phrase in q for phrase in region_ranking):
        sql = f"""
            SELECT region
            FROM orders
            WHERE {where_clause}
            GROUP BY region
            ORDER BY SUM(amount_usd) DESC, region ASC
            LIMIT 1
        """

        return sql, params, "string"

    # --------------------------------------------------------
    # Distinct customers.
    # --------------------------------------------------------

    distinct_customer_phrases = (
        "distinct customer",
        "different customer",
        "unique customer",
        "how many customers",
        "number of customers",
        "count customers",
        "count of customers",
    )

    if any(phrase in q for phrase in distinct_customer_phrases):
        sql = f"""
            SELECT COUNT(DISTINCT customer)
            FROM orders
            WHERE {where_clause}
        """

        return sql, params, "int"

    # --------------------------------------------------------
    # Count orders.
    # --------------------------------------------------------

    order_count_phrases = (
        "how many orders",
        "number of orders",
        "count orders",
        "count of orders",
        "total orders",
        "number of refunded",
        "how many refunded",
        "how many voided",
        "how many cancelled",
        "how many canceled",
    )

    if any(phrase in q for phrase in order_count_phrases):
        sql = f"""
            SELECT COUNT(*)
            FROM orders
            WHERE {where_clause}
        """

        return sql, params, "int"

    # --------------------------------------------------------
    # Total quantity / units.
    # --------------------------------------------------------

    quantity_phrases = (
        "how many units",
        "total quantity",
        "total units",
        "quantity sold",
        "units sold",
    )

    if any(phrase in q for phrase in quantity_phrases):
        sql = f"""
            SELECT COALESCE(SUM(qty), 0)
            FROM orders
            WHERE {where_clause}
        """

        return sql, params, "int"

    # --------------------------------------------------------
    # Average order value.
    # --------------------------------------------------------

    if "average" in q or "mean" in q:
        sql = f"""
            SELECT ROUND(COALESCE(AVG(amount_usd), 0), 2)
            FROM orders
            WHERE {where_clause}
        """

        return sql, params, "money"

    # --------------------------------------------------------
    # Generic revenue.
    # --------------------------------------------------------

    revenue_words = (
        "revenue",
        "earnings",
        "earned",
        "total sales",
        "sales in usd",
        "total income",
        "generated",
        "how much",
        "total amount",
    )

    if any(word in q for word in revenue_words):
        sql = f"""
            SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2)
            FROM orders
            WHERE {where_clause}
        """

        return sql, params, "money"

    # --------------------------------------------------------
    # Fallback.
    # --------------------------------------------------------

    sql = f"""
        SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2)
        FROM orders
        WHERE {where_clause}
    """

    return sql, params, "money"


# ============================================================
# ANSWER GENERATION
# ============================================================

def answer_question(question):
    sql, params, answer_type = parse_question_to_sql(question)

    print(f"\n[QUESTION] {question}")
    print(f"[SQL] {sql.strip()}")
    print(f"[PARAMS] {params}")

    result = execute_scalar(sql, params)

    if answer_type == "string":
        answer = str(result)

    elif answer_type == "int":
        answer = int(result)

    else:
        answer = round(float(result), 2)

    print(f"[ANSWER] {answer}")

    return answer


# ============================================================
# API ENDPOINTS
# ============================================================

@app.on_event("startup")
async def startup_event():
    init_database()


@app.get("/")
async def root():
    return {
        "service": "Acme Appliances Ledger Agent",
        "status": "ok" if database_ready else "initializing",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok" if database_ready else "not_ready",
        "orders_loaded": orders_loaded,
        "last_refresh": last_refresh,
    }


@app.post("/refresh")
async def refresh():
    try:
        init_database()

        return {
            "status": "ok",
            "orders_loaded": orders_loaded,
            "last_refresh": last_refresh,
        }

    except Exception as exc:
        print(f"[REFRESH ERROR] {exc}")

        raise HTTPException(
            status_code=500,
            detail="Could not refresh the ledger.",
        )


@app.post("/")
@app.post("/answer")
async def answer_endpoint(request: Request):
    if not database_ready:
        raise HTTPException(
            status_code=503,
            detail="Ledger database is not ready.",
        )

    try:
        body = await request.json()

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Request body must be valid JSON.",
        )

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail="Request body must be a JSON object.",
        )

    question = body.get("question")

    if not isinstance(question, str) or not question.strip():
        raise HTTPException(
            status_code=400,
            detail="Provide a non-empty 'question' field.",
        )

    try:
        result = answer_question(question.strip())
        return {"answer": result}

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except Exception as exc:
        print(f"[ANSWER ERROR] {type(exc).__name__}: {exc}")

        raise HTTPException(
            status_code=500,
            detail="Could not answer the question.",
        )


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )
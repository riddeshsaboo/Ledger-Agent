import json
import os
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

BASE_URL = (
    "https://exam.sanand.workers.dev/questionData"
    "?email=24f3001311%40ds.study.iitm.ac.in"
    "&quizSign=qaDMHhDfPMnBJcU%2BFeBuxXtwY7xWzn3xG%2F3dOYEufQcegy95Ub5yeb2%2FcMsgy1taz0jK9uOI9%2FLP49TGZHoOvo04ZWv7CpLwoYiKXSq4h1nBHTQfKO1%2BtDiuqYcmj%2B6wrkOl80aCLFD9B6R7n2PScSGSOeOMsUWcrlzyq0qZ0gSLHMT%2BUpendMsv3NCJy%2FATctMHijhyYtfQpmXa4tQicu9XC3%2FjXGTt4G1OYjw6VOEZCdv3a948waETDJTLl164m6ZyTKd2KU16ha1kCflXNERb4Lpfx66566JYAKaXe0zSgFP0xS9Xl%2FP1ENC0C8qiB1vagsgvfENaUfLkKf0m7w%3D%3D"
    "&questionId=q-ledger-agent-server"
)
RATES_URL = BASE_URL + "&path=%2Frates"
EXPORT_URL = BASE_URL + "&path=%2Fexport"
TZ = ZoneInfo("Asia/Kolkata")
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36"}
MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}
PRODUCTS = ["Air Fryer", "Rice Cooker", "Juicer", "Toaster",
            "Mixer", "Kettle", "Blender", "Grinder"]
REGIONS = ["North", "South", "East", "West", "Central"]

app = FastAPI(title="Acme Appliances Ledger Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
lock = threading.RLock()
conn = None
ready = False
orders_loaded = 0
last_refresh = None


def init_database():
    global conn, ready, orders_loaded, last_refresh

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        rr = client.get(RATES_URL, headers=HEADERS)
        rr.raise_for_status()
        rates = rr.json()["usd_per_unit"]
        er = client.get(EXPORT_URL, headers=HEADERS)
        er.raise_for_status()
        raw = er.text.strip()

    if not raw:
        raise RuntimeError("The ledger export is empty.")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    df = pd.DataFrame(rows)
    required = {"id", "customer", "region", "product", "qty", "amount",
                "currency", "status", "created_at", "updated_at"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing ledger columns: {sorted(missing)}")

    df["created_at_utc"] = pd.to_datetime(df["created_at"], utc=True,
                                           format="ISO8601", errors="coerce")
    df["updated_at_utc"] = pd.to_datetime(df["updated_at"], utc=True,
                                           format="ISO8601", errors="coerce")
    if df["created_at_utc"].isna().any() or df["updated_at_utc"].isna().any():
        raise RuntimeError("Invalid timestamps found in ledger.")

    # Keep the most recently updated row for each order ID.
    df = df.sort_values("updated_at_utc", kind="mergesort")
    df = df.drop_duplicates(subset=["id"], keep="last").copy()

    for col in ["customer", "region", "product", "currency", "status"]:
        df[col] = df[col].astype(str).str.strip()
    df["currency"] = df["currency"].str.upper()
    df["status"] = df["status"].str.lower()
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    if df["qty"].isna().any() or df["amount"].isna().any():
        raise RuntimeError("Invalid numeric values found in ledger.")

    local = df["created_at_utc"].dt.tz_convert(TZ)
    df["created_date"] = local.dt.strftime("%Y-%m-%d")
    df["created_month"] = local.dt.strftime("%Y-%m")
    df["created_year"] = local.dt.year.astype(int)
    df["rate_to_usd"] = df["currency"].map(rates)
    if df["rate_to_usd"].isna().any():
        bad = sorted(df.loc[df["rate_to_usd"].isna(), "currency"].unique())
        raise RuntimeError(f"Missing USD rates for currencies: {bad}")
    df["amount_usd"] = df["amount"] * df["rate_to_usd"]
    df = df.drop(columns=["created_at_utc", "updated_at_utc"])

    new_conn = sqlite3.connect(":memory:", check_same_thread=False)
    df.to_sql("orders", new_conn, index=False, if_exists="replace")
    new_conn.execute("CREATE INDEX idx_status ON orders(status)")
    new_conn.execute("CREATE INDEX idx_month ON orders(created_month)")
    new_conn.execute("CREATE INDEX idx_product ON orders(product)")
    new_conn.commit()

    with lock:
        if conn is not None:
            conn.close()
        conn = new_conn
        orders_loaded = len(df)
        last_refresh = datetime.now(TZ).isoformat()
        ready = True
    print(f"Ledger ready: {orders_loaded} current orders.")


def build_filters(question):
    q = question.lower()
    conditions, params = [], []

    if re.search(r"\b(refund(?:ed|s)?)\b", q):
        conditions.append("status = ?")
        params.append("refunded")
    elif re.search(r"\b(void(?:ed)?|cancel(?:led|ed)?)\b", q):
        conditions.append("status = ?")
        params.append("void")
    elif re.search(r"\b(all|every)\s+(orders?|transactions?)\b", q):
        pass
    else:
        conditions.append("status = ?")
        params.append("paid")

    for region in REGIONS:
        if re.search(rf"\b{re.escape(region.lower())}\b", q):
            conditions.append("region = ?")
            params.append(region)
            break

    for product in sorted(PRODUCTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(product.lower())}s?\b", q):
            conditions.append("product = ?")
            params.append(product)
            break

    customer = re.search(r"\b(c\d{4})\b", q, re.I)
    if customer:
        conditions.append("customer = ?")
        params.append(customer.group(1).upper())

    iso_date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if iso_date:
        conditions.append("created_date = ?")
        params.append(iso_date.group(1))
    else:
        month = next((n for name, n in MONTHS.items()
                      if re.search(rf"\b{name}\b", q)), None)
        year_match = re.search(r"\b(20\d{2})\b", q)
        year = int(year_match.group(1)) if year_match else None
        if month:
            conditions.append("created_month = ?")
            params.append(f"{year or 2026}-{month}")
        elif year and re.search(r"\b(year|annual|yearly)\b", q):
            conditions.append("created_year = ?")
            params.append(year)

    return (" AND ".join(conditions) if conditions else "1=1"), params


def make_plan(question):
    q = re.sub(r"\s+", " ", question.lower()).strip()
    if not q:
        raise ValueError("Question cannot be empty.")
    where, params = build_filters(question)

    product_rank = any(s in q for s in (
        "which product", "what product", "top-selling product",
        "top selling product", "best-selling product", "best selling product",
        "product earned the most", "product generated the most",
        "highest revenue product", "highest-revenue product",
        "most revenue product", "product sold the most",
    ))
    customer_rank = any(s in q for s in (
        "which customer", "what customer", "top customer",
        "customer spent the most", "customer spending the most",
        "highest spending customer", "biggest spender",
    ))
    region_rank = any(s in q for s in (
        "which region", "what region", "top region",
        "region earned the most", "region generated the most",
        "highest revenue region",
    ))
    revenue_words = any(s in q for s in (
        "revenue", "earned", "earnings", "sales value", "in usd",
        "amount", "spending", "spent",
    ))

    if product_rank:
        metric = "SUM(amount_usd)" if revenue_words else "SUM(qty)"
        if any(s in q for s in ("quantity", "units sold", "number of units", "most units")):
            metric = "SUM(qty)"
        direction = "ASC" if any(s in q for s in ("lowest", "least", "fewest")) else "DESC"
        return (
            f"SELECT product FROM orders WHERE {where} "
            f"GROUP BY product ORDER BY {metric} {direction}, product ASC LIMIT 1",
            params, "string"
        )

    if customer_rank:
        return (
            f"SELECT customer FROM orders WHERE {where} "
            "GROUP BY customer ORDER BY SUM(amount_usd) DESC, customer ASC LIMIT 1",
            params, "string"
        )

    if region_rank:
        return (
            f"SELECT region FROM orders WHERE {where} "
            "GROUP BY region ORDER BY SUM(amount_usd) DESC, region ASC LIMIT 1",
            params, "string"
        )

    # Average-value intent must be checked BEFORE generic "how many".
    # Example: "On average, how many US dollars is a paid South order worth?"
    # asks for AVG(amount_usd), not COUNT(*).
    average_intent = (
        "average" in q
        or "on average" in q
        or "mean" in q
        or "worth" in q
        or bool(re.search(r"\bavg\b", q))
        or bool(re.search(r"\b(per|each)\s+order\b", q))
    )
    if average_intent:
        sql = (
            f"SELECT ROUND(COALESCE(AVG(amount_usd), 0), 2) "
            f"FROM orders WHERE {where}"
        )
        return sql, params, "money"

    # Distinct-customer questions must be checked before generic counts.
    if (re.search(r"\b(unique|distinct|different)\s+customers?\b", q)
        or re.search(r"\bhow many customers?\b", q)
        or re.search(r"\bcount\s+(?:the\s+)?customers?\b", q)
        or re.search(r"\bnumber of customers?\b", q)):
        return f"SELECT COUNT(DISTINCT customer) FROM orders WHERE {where}", params, "int"

    # Count intent: "how many" means a count only when it is not an
    # average/value question. This handles "Count the refunded West orders".
    if (re.search(r"\b(count|counting|number of|how many)\b", q)
        or re.search(r"\btotal number of\b", q)):
        return f"SELECT COUNT(*) FROM orders WHERE {where}", params, "int"

    if any(s in q for s in (
        "how many units", "total quantity", "total units", "quantity sold",
        "units sold", "sum of qty", "total qty",
    )):
        return f"SELECT COALESCE(SUM(qty), 0) FROM orders WHERE {where}", params, "int"

    if any(s in q for s in (
        "revenue", "earnings", "earned", "total sales", "sales in usd",
        "total income", "generated", "total amount", "how much",
    )):
        return (
            f"SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2) FROM orders WHERE {where}",
            params, "money"
        )

    # Default metric: total paid revenue for the extracted filters.
    return (
        f"SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2) FROM orders WHERE {where}",
        params, "money"
    )


def answer_question(question):
    sql, params, kind = make_plan(question)
    print(f"[QUESTION] {question}\n[SQL] {sql}\n[PARAMS] {params}")
    with lock:
        row = conn.execute(sql, params).fetchone()
    value = row[0] if row and row[0] is not None else 0
    if kind == "string":
        return str(value) if value else "No matching result"
    if kind == "int":
        return int(value)
    return round(float(value), 2)


@app.on_event("startup")
async def startup_event():
    init_database()


@app.get("/")
async def root():
    return {"service": "Acme Appliances Ledger Agent",
            "status": "ok" if ready else "not_ready"}


@app.get("/health")
async def health():
    return {"status": "ok" if ready else "not_ready",
            "orders_loaded": orders_loaded, "last_refresh": last_refresh}


@app.post("/refresh")
async def refresh():
    try:
        init_database()
        return {"status": "ok", "orders_loaded": orders_loaded,
                "last_refresh": last_refresh}
    except Exception as exc:
        print(f"[REFRESH ERROR] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Ledger refresh failed.")


@app.post("/")
@app.post("/answer")
async def answer_endpoint(request: Request):
    if not ready:
        raise HTTPException(status_code=503, detail="Ledger is not ready.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(status_code=400, detail="Provide a non-empty question.")
    try:
        return {"answer": answer_question(question.strip())}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        print(f"[ANSWER ERROR] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Unable to answer the question.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

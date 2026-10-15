import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
RATES = {
    "USD": 1.0,
    "EUR": 1.03,
    "INR": 0.012,
}

orders_by_id = {}

with open("export.ndjson", "r", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue

        order = json.loads(line)
        order_id = order["id"]

        # Keep the latest version of each order.
        previous = orders_by_id.get(order_id)
        if previous is None or order["updated_at"] > previous["updated_at"]:
            orders_by_id[order_id] = order

revenue_by_product = defaultdict(float)
orders_by_product = defaultdict(int)

for order in orders_by_id.values():
    if order.get("status", "").lower() != "paid":
        continue

    # Use the order creation date in the business timezone.
    created_at = datetime.fromisoformat(
        order["created_at"].replace("Z", "+00:00")
    )

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=BUSINESS_TZ)

    created_at = created_at.astimezone(BUSINESS_TZ)

    if created_at.year != 2026 or created_at.month != 3:
        continue

    currency = order.get("currency", "USD").upper()
    rate = RATES.get(currency)

    if rate is None:
        print(f"Unknown currency: {currency}")
        continue

    amount = float(order.get("amount", 0))
    revenue_by_product[order["product"]] += amount * rate
    orders_by_product[order["product"]] += 1

print("\nMarch 2026 — paid revenue by product (USD):\n")

for product, revenue in sorted(
    revenue_by_product.items(),
    key=lambda item: item[1],
    reverse=True,
):
    print(
        f"{product:15s} "
        f"${revenue:12,.2f} "
        f"({orders_by_product[product]} orders)"
    )

if revenue_by_product:
    print("\nTOP-SELLING PRODUCT:", max(revenue_by_product, key=revenue_by_product.get))
else:
    print("No matching paid orders found.")

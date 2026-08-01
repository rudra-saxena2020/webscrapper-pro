
from db import get_connection
conn, cur = get_connection()
cur.execute("SELECT website_url, total_products FROM products")
rows = cur.fetchall()
print(f"Total rows in DB: {len(rows)}")
for url, count in rows:
    print(f"- '{url}': {count} products")


from db import get_connection
conn, cur = get_connection()
cur.execute("SELECT website_url, total_products FROM products")
rows = cur.fetchall()
with open("db_inventory.txt", "w", encoding="utf-8") as f:
    f.write(f"Total rows in DB: {len(rows)}\n")
    for url, count in rows:
        f.write(f"- '{url}': {count} products\n")
conn.close()

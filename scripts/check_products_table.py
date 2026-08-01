import sys
import os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from db import get_connection

print("--- PRODUCTS TABLE ---")
conn, cur = get_connection()
cur.execute("SELECT website_url, total_products, updated_at FROM products")
rows = cur.fetchall()
for row in rows:
    print(f"URL: {row[0]}, Count: {row[1]}, Updated: {row[2]}")
cur.close()
conn.close()

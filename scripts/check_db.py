import db
import os
from dotenv import load_dotenv

load_dotenv()

def check_status():
    conn, cursor = db.get_connection()
    
    # Check total products
    cursor.execute("SELECT count(*) FROM products")
    total_products = cursor.fetchone()[0]
    print(f"Total Products in DB: {total_products}")
    
    # Check latest scrapes
    print("\nLatest 5 Scrapes:")
    cursor.execute("SELECT id, scraper_id, status, products_count, started_at FROM scrapes ORDER BY started_at DESC LIMIT 5")
    scrapes = cursor.fetchall()
    for row in scrapes:
        print(row)
        
    cursor.close()
    conn.close()

if __name__ == "__main__":
    check_status()

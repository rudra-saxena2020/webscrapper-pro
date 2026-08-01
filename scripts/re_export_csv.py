import json
import os
import sys
from datetime import datetime

# Add root to sys.path
sys.path.append(os.getcwd())

from db import get_product_by_website, upload_csv_to_supabase, start_scrape_record, update_scrape_record
from shopify_transformer import transform_to_shopify, export_shopify_csv

def re_export():
    scraper_id = "cruise_fashion"
    print(f"📦 Fetching data for '{scraper_id}' from database...")
    
    # 1. Get products from database
    row = get_product_by_website(scraper_id)
    if not row:
        print(f"❌ No products found in DB for '{scraper_id}'.")
        return
        
    products_data = row.get("products", {})
    if isinstance(products_data, str):
        products_data = json.loads(products_data)
        
    # Some versions store { "products": [...] }, others just [...]
    if isinstance(products_data, dict):
        products = products_data.get("products", [])
    else:
        products = products_data
    
    if not products:
        print("❌ Product list is empty.")
        return
        
    print(f"✅ Found {len(products)} products. Applying NEW pricing engine...")
    
    # 2. Transform (this applies the new pricing logic and logging)
    rows = transform_to_shopify(products)
    
    # 3. Export CSV locally
    csv_filename = f"scraped_files/{scraper_id}_latest.csv"
    os.makedirs("scraped_files", exist_ok=True)
    export_shopify_csv(rows, csv_filename)
    
    # 4. Upload to Supabase so the "Download CSV" button works on Dashboard
    print(f"🚀 Uploading fixed CSV to Supabase Storage...")
    csv_url = upload_csv_to_supabase(csv_filename, scraper_id)
    
    # 5. Update a fresh record so it's top of the list in "Recent Scrapes"
    record_id = start_scrape_record(scraper_id)
    update_scrape_record(
        record_id, 
        status="completed", 
        products_count=len(products), 
        csv_url=csv_url
    )
    
    print(f"\n✨ SUCCESS! Corrected CSV generated and updated in Dashboard.")
    print(f"🔗 View here: {csv_url}")

if __name__ == "__main__":
    re_export()

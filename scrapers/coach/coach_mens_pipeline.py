import os
import time
import json
import csv
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cur_requests
from seleniumbase import SB
from dotenv import load_dotenv

# Import our robust core components
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from core.tag_engine import clean_title, generate_handle, apply_standardized_tags, detect_gender, append_brand_message, sanitize_html_description
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.db import upsert_all_product_data, start_scrape_record, update_scrape_record, heartbeat_scrape_record, upload_csv_to_supabase

load_dotenv()

# --- CONFIGURATION ---
INPUT_LINKS = [
    "https://www.coach.com/shop/outlet/women/view-all?sz=1000",
    "https://www.coach.com/shop/outlet/men/view-all?sz=1000",
    "https://www.coach.com/shop/sale/view-all?gender=Women&sz=1000",
    "https://www.coach.com/shop/sale/view-all?gender=Men&sz=1000"
]

SCRAPER_ID = "coach_mens_exclusive"
CONCURRENCY = 20 # High speed fetching
PROXY = None
# PROXY = os.getenv("PROXY_URL") or os.getenv("PROXY_CHROME")
IF_PROXY = {"http": PROXY, "https": PROXY} if PROXY else None

class CoachMensPipeline:
    def __init__(self):
        self.products_discovered = []
        self.products_cleaned = []
        self.stats = {
            "total_scraped": 0,
            "total_men": 0,
            "total_rejected": 0,
            "missing_data_fields": 0,
            "total_fields_checked": 0
        }
        self.seen_ids = set()
        self.lock = threading.Lock()

    def discover_ids(self):
        """Phase 1: Discover all product IDs from the target links with gender hints."""
        print("🚀 Starting discovery for collections with Cruise-style gender hints...")
        id_to_hint = {}
        
        with SB(uc=True, headless=True, proxy=PROXY) as sb:
            for url in INPUT_LINKS:
                # Detect gender hint from URL (Cruise Logic)
                url_lower = url.lower()
                gender_hint = "women" if "/women/" in url_lower or "gender=women" in url_lower else "men"
                
                print(f"🔍 Crawling: {url} (Hint: {gender_hint})")
                try:
                    sb.open(url)
                    time.sleep(5)
                    
                    last_count = 0
                    no_change_count = 0
                    while no_change_count < 3:
                        try:
                            sb.execute_script("window.scrollTo(0, Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 0));")
                        except: pass
                        time.sleep(3)
                        try:
                            if sb.is_element_visible('button.load-more'):
                                sb.click('button.load-more')
                                time.sleep(2)
                        except: pass
                        
                        current_source = sb.get_page_source()
                        pids = set(re.findall(r'"id":"([^"]+)"', current_source))
                        pids.update(re.findall(r'data-pid="([^"]+)"', current_source))
                        current_count = len(pids)
                        
                        if current_count > last_count:
                            print(f"  📈 Found {current_count} IDs so far...")
                            last_count = current_count
                            no_change_count = 0
                        else:
                            no_change_count += 1
                    
                    final_source = sb.get_page_source()
                    pids = set(re.findall(r'"sku":"([^"\s]+)\s*[^"]*"', final_source))
                    pids.update(re.findall(r'"id":"([^"]+)"', final_source))
                    pids.update(re.findall(r'data-pid="([^"]+)"', final_source))
                    
                    for p in pids:
                        base_p = p.split(' ')[0].split('+')[0]
                        if len(base_p) > 2:
                            id_to_hint[base_p] = gender_hint
                    
                    print(f"  ✅ Finished {url}. Total found: {len(pids)}")
                except Exception as e:
                    print(f"  ❌ Error crawling {url}: {e}")
        
        print(f"🏁 Discovery complete. Total unique IDs: {len(id_to_hint)}")
        return id_to_hint

    def fetch_product_details(self, pid, sb):
        """Phase 2: Use the existing SB browser to fetch API JSON (Bypass Akamai)."""
        api_url = f"https://www.coach.com/on/demandware.store/Sites-Coach_US-Site/en_US/Product-Variation?pid={pid}&dwvar_{pid}_color=null"
        try:
            sb.open(api_url)
            text = sb.get_text('body')
            data = json.loads(text)
            product_data = data.get('product', {})
            return product_data
        except Exception as e:
            return None

    def calculate_gender_score(self, raw_data, product_url):
        """Scoring engine based on strict signals."""
        score = 0
        
        title = raw_data.get('productName', '').lower()
        desc = (raw_data.get('longDescription', '') or raw_data.get('shortDescription', '') or "").lower()
        cat_text = " ".join([c.get('name', '').lower() for c in raw_data.get('categories', [])])
        url_lower = product_url.lower()

        # POSITIVE SIGNALS
        if "/men/" in url_lower: score += 5
        if "gender=men" in url_lower: score += 5
        if "men" in cat_text: score += 4
        if re.search(r'\bmen\b', title): score += 3
        if re.search(r'\bmen\b', desc): score += 2

        # NEGATIVE SIGNALS
        if "/women/" in url_lower: score -= 5
        if "gender=women" in url_lower: score -= 5
        if re.search(r'\bwomen\b', title): score -= 3
        if re.search(r'\bwomen\b', desc): score -= 3

        return score

    def process_and_filter(self, raw_data, gender_hint=None):
        """Phase 2 & 3: Cruise-style Gender Detection & Cleaning."""
        if not raw_data: return None
        
        pid = raw_data.get('id')
        product_url = f"https://www.coach.com/products/{pid}.html"
        
        # --- GENDER SCORING ENGINE (STRICT) ---
        score = self.calculate_gender_score(raw_data, product_url)
        
        # CRUISE LOGIC: Favor source hint if available
        # But if source is 'women' link, we reject for this Men-only pipeline
        if gender_hint == "women" or score < 5:
            self.lock.acquire()
            self.stats["total_rejected"] += 1
            self.lock.release()
            return None
        
        # --- CLEANING & STRUCTURING ---
        title = raw_data.get('productName', '')
        cleaned_title = clean_title(title)
        
        # Description
        raw_desc = raw_data.get('longDescription', '')
        if not raw_desc: raw_desc = raw_data.get('shortDescription', '')
        body_html = append_brand_message(f"<div>{sanitize_html_description(raw_desc)}</div>")
        
        # Price
        price_obj = raw_data.get('price', {})
        list_price = price_obj.get('list', {}).get('value', 0)
        sale_price = price_obj.get('sales', {}).get('value', list_price)
        
        # Images
        images_data = raw_data.get('images', {}).get('large', [])
        image_urls = [img.get('url') for img in images_data if img.get('url')]
        
        # Category
        primary_cat = raw_data.get('primaryCategory', {}).get('name', 'Accessories')
        
        # Tags Logic
        tag_meta = {
            "Title": title,
            "Product Category": primary_cat,
            "Type": primary_cat,
            "Vendor": "Coach",
            "description": raw_desc,
            "url": product_url
        }
        raw_tags = apply_standardized_tags(tag_meta, gender_hint="men")
        
        # We append minimal essential identifiers for Shopify, keeping everything else raw.
        final_tags = f"{raw_tags}, Gender: Men, Brand: Coach"

        product_obj = {
            "Handle": generate_handle(cleaned_title, pid),
            "Title": cleaned_title,
            "Body (HTML)": body_html,
            "Vendor": "Coach",
            "Product Category": primary_cat,
            "Type": primary_cat,
            "Tags": final_tags,
            "Published": "TRUE",
            "Option1 Name": "Title",
            "Option1 Value": "Default Title",
            "Variant SKU": f"{pid}-OS",
            "Variant Price": sale_price,
            "Variant Compare At Price": list_price if list_price > sale_price else "",
            "Variant Inventory Qty": 100,
            "images": image_urls,
            "_raw_score": score
        }
        return product_obj

    def fetch_and_clean_worker(self, id_hint_batch):
        """Worker function for parallel SB fetching with hints."""
        results = []
        with SB(uc=True, headless=True, proxy=PROXY) as sb:
            sb.open("https://www.coach.com")
            time.sleep(5)
            
            for pid, hint in id_hint_batch:
                try:
                    product_data = self.fetch_product_details(pid, sb)
                    if product_data:
                        cleaned = self.process_and_filter(product_data, gender_hint=hint)
                        if cleaned:
                            results.append(cleaned)
                    else:
                        # Mark as rejected if fetch failed
                        with self.lock: self.stats["total_rejected"] += 1
                except Exception: continue
        return results

    def run(self):
        """Main orchestrator for high-performance scraping pipeline."""
        start_time = time.time()
        
        # Phase 1: Discovery
        id_to_hint = self.discover_ids()
        if not id_to_hint:
            print("❌ No products discovered. Exiting.")
            return None
            
        pids_with_hints = list(id_to_hint.items())
        total_pids = len(pids_with_hints)
        self.stats["total_scraped"] = total_pids
        
        # Split into batches for parallel workers
        num_workers = 4
        batches = [pids_with_hints[i::num_workers] for i in range(num_workers)]
        
        print(f"🧵 Processing {total_pids} products with parallel browser fetchers...")
        
        # Initialize CSV with Shopify headers
        csv_path = f"scraped_files/coach_mens_shopify_final.csv"
        os.makedirs("scraped_files", exist_ok=True)
        fieldnames = [
            "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags",
            "Published", "Option1 Name", "Option1 Value", "Variant SKU", "Variant Price",
            "Variant Compare At Price", "Variant Inventory Qty", "Image Src"
        ]
        
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

        all_results = []
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(self.fetch_and_clean_worker, b) for b in batches]
            for future in as_completed(futures):
                batch_results = future.result()
                if batch_results:
                    all_results.extend(batch_results)
                    
                    # Incremental write batch to CSV
                    with open(csv_path, 'a', newline='', encoding='utf-8-sig') as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        for p in batch_results:
                            # Handle multiple images as multiple rows for Shopify
                            for i, img in enumerate(p["images"]):
                                row = {
                                    "Handle": p["Handle"],
                                    "Title": p["Title"] if i == 0 else "",
                                    "Body (HTML)": p["Body (HTML)"] if i == 0 else "",
                                    "Vendor": p["Vendor"] if i == 0 else "",
                                    "Product Category": p["Product Category"] if i == 0 else "",
                                    "Type": p["Type"] if i == 0 else "",
                                    "Tags": p["Tags"] if i == 0 else "",
                                    "Published": p["Published"] if i == 0 else "",
                                    "Option1 Name": p["Option1 Name"] if i == 0 else "",
                                    "Option1 Value": p["Option1 Value"] if i == 0 else "",
                                    "Variant SKU": p["Variant SKU"] if i == 0 else "",
                                    "Variant Price": p["Variant Price"] if i == 0 else "",
                                    "Variant Compare At Price": p.get("Variant Compare At Price", "") if i == 0 else "",
                                    "Variant Inventory Qty": p["Variant Inventory Qty"] if i == 0 else "",
                                    "Image Src": img
                                }
                                writer.writerow(row)
                    print(f"  📦 Batch finished ({len(batch_results)} products). Progress: {len(all_results)} saved.")

        results = all_results
        
        # Deduplication (in memory for final stats, but CSV is already written)
        unique_results = []
        seen_handles = set()
        for res in results:
            if res["Handle"] not in seen_handles:
                seen_handles.add(res["Handle"])
                unique_results.append(res)
        
        self.products_cleaned = unique_results
        self.stats["total_men"] = len(unique_results)
        
        # Save to Database
        print(f"💾 Saving {len(unique_results)} Men's products to database...")
        upsert_all_product_data(unique_results, SCRAPER_ID, "USD")
        
        # Print Final Stats
        missing_pct = (self.stats["missing_data_fields"] / self.stats["total_fields_checked"] * 100) if self.stats["total_fields_checked"] > 0 else 0
        usable_pct = (self.stats["total_men"] / self.stats["total_scraped"] * 100) if self.stats["total_scraped"] > 0 else 0
        
        print("\n" + "="*45)
        print("🎉 PRODUCTION-READY COACH CLASSIFICATION COMPLETE")
        print("="*45)
        print(f"📦 Total Scraped:                {self.stats['total_scraped']}")
        print(f"✅ Accepted (MEN):               {self.stats['total_men']}")
        print(f"❌ Rejected (WOMEN/UNKNOWN):      {self.stats['total_rejected']}")
        print(f"📊 Usable Data:                  {usable_pct:.2f}%")
        print(f"📉 Data Quality (Missing):        {missing_pct:.2f}%")
        print(f"📁 Shopify CSV:                  {csv_path}")
        print("="*45)
        
        return csv_path

if __name__ == "__main__":
    pipeline = CoachMensPipeline()
    pipeline.run()

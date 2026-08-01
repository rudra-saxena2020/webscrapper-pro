import os
import sys
from scrapers.cruise_fashion.cruise_fashion import complete_workflow_cruise_fashion

# Ensure we're in the right directory
os.chdir(os.path.abspath(os.path.dirname(__file__)))

if __name__ == "__main__":
    print("🚀 Starting localized run for Cruise Fashion...")
    results = complete_workflow_cruise_fashion()
    
    if results:
        print(f"✅ Successfully scraped {len(results)} products!")
    else:
        print("❌ Scraper finished but returned 0 products. See logs above.")

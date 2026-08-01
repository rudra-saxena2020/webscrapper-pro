import asyncio
import inspect
from scrapers.cruise_fashion.cruise_fashion import complete_workflow_cruise_fashion
from scrapers.coach.coach import complete_workflow_coach
from scrapers.michael_kors.michael_kors import complete_workflow_michael_kors
from scrapers.karl.karl import complete_workflow_karl
from scrapers.marcjacobs.marcjacobs import complete_workflow_marcjacobs
from scrapers.tory.tory import complete_workflow_tory
from scrapers.gymshark.gymshark import complete_workflow_gymshark
from scrapers.aloyoga.aloyoga import complete_workflow_aloyoga
from scrapers.mytheresa.mytheresa import complete_workflow_mytheresa
from scrapers.thedesignerboxuk.thedesignerboxuk import complete_workflow_thedesignerboxuk
from scrapers.uk_polene.uk_polene import complete_workflow_uk_polene
from scrapers.hoka.hoka import complete_workflow_hoka
from scrapers.drmartens.drmartens import complete_workflow_drmartens
from scrapers.ugg.ugg import complete_workflow_ugg
from scrapers.organicbasics.organicbasics import complete_workflow_organicbasics
from scrapers.skims.skims import complete_workflow_skims
from scrapers.thereformation.thereformation import complete_workflow_thereformation
from scrapers.underarmour.underarmour import complete_workflow_underarmour
from scrapers.stanley.stanley import complete_workflow_stanley1913
from scrapers.gemopticians.gemopticians import complete_workflow_gemopticians
from scrapers.katspade_outlet.kateoutlet import complete_workflow_kate_outlet
from scrapers.jwpei.jwpei import complete_workflow_jwpei

try:
    from color_maps import run_color_mapping
except ImportError:
    print("Warning: color_maps module not found. Color mapping will be skipped")
    def run_color_mapping():
        print("Color mapping skipped - module not available")
        pass

def not_run():
    """Placeholder for scrapers not to be run"""
    pass
import json
import os

def get_available_scrapers():
    """Read all registered scrapers from JSON and return a dictionary"""
    registry_path = os.path.join(os.path.dirname(__file__), "scrapers_registry.json")
    scrapers = {
        "cruise_fashion":     ("Cruise Fashion",        complete_workflow_cruise_fashion),
        "coach":              ("Coach",                 complete_workflow_coach),
        "michael_kors":       ("Michael Kors",          complete_workflow_michael_kors),
        "karl":               ("Karl Lagerfeld",        complete_workflow_karl),
        "marcjacobs":         ("Marc Jacobs",           complete_workflow_marcjacobs),
        "tory":               ("Tory Burch",            complete_workflow_tory),
        "gymshark":           ("Gymshark",              complete_workflow_gymshark),
        "aloyoga":            ("Alo Yoga",              complete_workflow_aloyoga),
        "mytheresa":          ("MyTheresa",             complete_workflow_mytheresa),
        "thedesignerboxuk":   ("The Designer Box UK",   complete_workflow_thedesignerboxuk),
        "uk_polene":          ("Polene UK",             complete_workflow_uk_polene),
        "hoka":               ("Hoka",                  complete_workflow_hoka),
        "drmartens":          ("Dr. Martens",            complete_workflow_drmartens),
        "ugg":                ("UGG",                    complete_workflow_ugg),
        "organicbasics":      ("Organic Basics",         complete_workflow_organicbasics),
        "skims":              ("SKIMS Body",             complete_workflow_skims),
        "thereformation":     ("The Reformation",        complete_workflow_thereformation),
        "underarmour":        ("Under Armour",           complete_workflow_underarmour),
        "stanley1913":        ("Stanley 1913",           complete_workflow_stanley1913),
        "gemopticians":       ("GEM Opticians",          complete_workflow_gemopticians),
        "katespadeoutlet":    ("Kate Spade Outlet",      complete_workflow_kate_outlet),
        "jwpei":              ("JW PEI",                 complete_workflow_jwpei),
    }


    
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r") as f:
                data = json.load(f)
                for item in data:
                    if item["id"] in ["cruise_fashion", "coach", "michael_kors", "karl", "marcjacobs", "tory", "gymshark", "aloyoga"]: continue # Already hardcoded

                    
                    # For now, we only support adding variations of cruise_fashion logic
                    if item.get("type") == "cruise_fashion":
                        # Create a closure that captures the specific item data
                        # Note: we use default arguments to bind current loop values (scraper_id, base_url)
                        def make_scraper(s_id, b_url):
                            def custom_scraper(target_urls=None, **kwargs):
                                return complete_workflow_cruise_fashion(
                                    target_urls=[b_url] if b_url else None, 
                                    scraper_id=s_id,
                                    **kwargs
                                )
                            return custom_scraper
                        
                        scrapers[item["id"]] = (item["name"], make_scraper(item["id"], item.get("base_url")))
        except Exception as e:
            print(f"Error reading scraper registry: {e}")
            
    return scrapers


def run_selected_scrapers(scraper_ids=None, run_color_mapping_after=True, progress_callback=None, stop_event=None, **kwargs):
    """
    Run specific scrapers by their IDs with optional arguments
    """
    available_scrapers = get_available_scrapers()
    
    # If no specific scrapers requested, run all
    if not scraper_ids:
        scraper_ids = list(available_scrapers.keys())
    
    results = {
        'completed': [],
        'failed': [],
        'total': len(scraper_ids)
    }
    
    print(f"\n🚀 Starting {len(scraper_ids)} scraper(s)...")
    
    for i, scraper_id in enumerate(scraper_ids):
        # Stagger starts to prevent proxy saturation
        if i > 0:
            import time
            time.sleep(5)
        # Global check for cancellation
        if stop_event and stop_event.is_set():
            print(f"🛑 Scraping process cancelled by user. Skipping {scraper_id}...")
            results['failed'].append({
                'id': scraper_id,
                'name': scraper_id,
                'error': 'Cancelled'
            })
            continue

        if scraper_id not in available_scrapers:
            print(f"❌ Unknown scraper ID: {scraper_id}")
            results['failed'].append({
                'id': scraper_id,
                'name': scraper_id,
                'error': 'Unknown scraper ID'
            })
            continue
            
        scraper_name, scraper_function = available_scrapers[scraper_id]
        
        # Initial progress
        if progress_callback:
            progress_callback(scraper_id, 5, f"Starting {scraper_name}...")

        try:
            print(f"\n🔄 Starting {scraper_name} scraper...")
            
            # Pass progress callback and stop event to scraper if it supports it
            sig = inspect.signature(scraper_function)
            if 'progress_callback' in sig.parameters:
                # Create a wrapper that adapts to scraper-specific progress
                def scraper_cb(p, s, count=None):
                    if progress_callback:
                        progress_callback(scraper_id, p, s, count)
                kwargs['progress_callback'] = scraper_cb

            if 'stop_event' in sig.parameters:
                kwargs['stop_event'] = stop_event

            # Check if the scraper function is async (coroutine)
            if inspect.iscoroutinefunction(scraper_function):
                asyncio.run(scraper_function(**kwargs))
            else:
                scraper_function(**kwargs)
                
            print(f"✅ {scraper_name} scraper completed successfully")
            
            if progress_callback:
                progress_callback(scraper_id, 100, "Completed")
                
            results['completed'].append({
                'id': scraper_id,
                'name': scraper_name
            })
        except Exception as e:
            error_msg = str(e)
            if "Cancelled" in error_msg:
                print(f"🛑 {scraper_name} scraper cancelled")
                status = "Cancelled"
            else:
                print(f"❌ {scraper_name} scraper failed with error: {error_msg}")
                status = f"failed: {error_msg}"
            
            results['failed'].append({
                'id': scraper_id,
                'name': scraper_name,
                'error': error_msg
            })
            
            if progress_callback:
                progress_callback(scraper_id, 0, status)

            print(f"   Continuing with next scraper...")
    
    # Run color mapping if requested AND NOT cancelled
    if run_color_mapping_after and not (stop_event and stop_event.is_set()):
        try:
            print(f"\n🔄 Starting color mapping...")
            run_color_mapping()
            print(f"✅ Color mapping completed successfully")
            results['color_mapping'] = 'success'
        except Exception as e:
            error_msg = str(e)
            print(f"❌ Color mapping failed with error: {error_msg}")
            results['color_mapping'] = f'failed: {error_msg}'
    
    return results


def run_all_scrapers(stop_event=None):
    """Run all available scrapers (legacy function for backward compatibility)"""
    return run_selected_scrapers(stop_event=stop_event)


if __name__ == "__main__":
    run_all_scrapers()
import pandas as pd
import re

def map_shopify_category(title, p_type, raw_category=""):
    text = f"{str(title)} {str(p_type)} {str(raw_category)}".lower()
    if any(k in text for k in ['shoe', 'sandal', 'boot', 'sneaker', 'heel', 'flat', 'trainer', 'footwear']):
        return "Apparel & Accessories > Shoes"
    if any(k in text for k in ['bag', 'handbag', 'tote', 'clutch', 'backpack', 'purse']):
        return "Apparel & Accessories > Handbags, Wallets & Cases > Handbags"
    if any(k in text for k in ['jacket', 'coat', 'blazer', 'outerwear']):
        return "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets"
    if any(k in text for k in ['sock', 'hosiery', 'tights']):
        return "Apparel & Accessories > Clothing > Underwear & Socks > Socks"
    if any(k in text for k in ['bra', 'panty', 'underwear', 'lingerie', 'thong', 'corsetry']):
        return "Apparel & Accessories > Clothing > Underwear & Socks"
    if any(k in text for k in ['dress', 'gown', 'skirt']):
        return "Apparel & Accessories > Clothing > Dresses"
    if any(k in text for k in ['top', 'shirt', 'blouse', 'tee', 't-shirt', 'hoodie', 'sweatshirt', 'knitwear', 'tracksuit']):
        return "Apparel & Accessories > Clothing > Tops"
    if any(k in text for k in ['pant', 'jean', 'trouser', 'legging', 'short']):
        return "Apparel & Accessories > Clothing > Pants"
    if 'accessories' in text:
        return "Apparel & Accessories"
    return "Apparel & Accessories > Clothing"

def fix_csv_categories(input_file, output_file):
    print(f"Reading {input_file}...")
    df = pd.read_csv(input_file)
    
    # We only apply mapping to rows that have a Title (usually the main product rows)
    # Actually, better to apply and propagate or just apply to all if it exists.
    # Shopify CSVs have the Category in the first row of each product.
    
    count = 0
    for idx, row in df.iterrows():
        title = row.get('Title', '')
        p_type = row.get('Type', '')
        raw_cat = row.get('Product Category', '')
        
        # Only update if Title is present (main row)
        if pd.notna(title) and str(title).strip() != "":
            fixed_cat = map_shopify_category(title, p_type, raw_cat)
            df.at[idx, 'Product Category'] = fixed_cat
            count += 1
            
    df = df.fillna("")
    df.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"Fixed {count} product categories. Saved to {output_file}")

if __name__ == "__main__":
    fix_csv_categories('scraped_files/cruise_fashion_latest.csv', 'scraped_files/cruise_fashion_fixed.csv')

import db

res = db.get_product_by_website('cruise_fashion')
if res:
    print(f"Website: {res.get('website_url')}")
    print(f"Total Products: {res.get('total_products')}")
    products = res.get('products')
    if isinstance(products, dict):
        p_list = products.get('products', [])
        print(f"Actual List Size: {len(p_list)}")
        if p_list:
            print(f"First Title: {p_list[0].get('Title')}")
else:
    print("No record found.")

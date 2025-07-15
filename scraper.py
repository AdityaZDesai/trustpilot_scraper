# trustpilot_scraper/scraper.py

import requests
from bs4 import BeautifulSoup
import json
import time
import pandas as pd
import logging
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv
import os
import sched
import threading

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise ValueError("MONGO_URL environment variable not set in .env file")

client = MongoClient(MONGO_URL)
db = client["test"]
business_collection = db["businesses"]
collection = db["reviews"]  # For storing reviews

# Scheduler setup
main_scheduler = sched.scheduler(time.time, time.sleep)
hourly_scheduler = sched.scheduler(time.time, time.sleep)
hourly_scrape_business_ids = set()

# Function to scrape all businesses every 5 minutes
last_enabled_status = {}
def periodic_scrape():
    all_businesses = list(business_collection.find({}))
    for business in all_businesses:
        try:
            trustpilot_settings = business.get("settings", {}).get("reviewPlatforms", {}).get("trustpilot", {})
            enabled = trustpilot_settings.get("enabled", False)
            link = trustpilot_settings.get("link", None)
            business_id = str(business.get('_id'))
            # Track previous enabled status
            was_enabled = last_enabled_status.get(business_id, False)
            last_enabled_status[business_id] = enabled
            if enabled and link:
                if business_id not in hourly_scrape_business_ids:
                    print(f"[IMMEDIATE] Scraping Trustpilot for business: {business.get('businessName', business.get('_id'))} ({link})")
                    scrape_trustpilot_reviews(link, business_id=business.get('_id'), business_name=business.get('businessName'))
                    hourly_scrape_business_ids.add(business_id)
                    # Schedule hourly scraping for this business
                    hourly_scheduler.enter(3600, 1, hourly_scrape, (business_id, link, business.get('businessName')))
            else:
                if business_id in hourly_scrape_business_ids:
                    hourly_scrape_business_ids.remove(business_id)
        except Exception as e:
            print(f"Error processing business {business.get('businessName', business.get('_id'))}: {e}")
    # Schedule next periodic scrape in 5 minutes
    main_scheduler.enter(300, 1, periodic_scrape)

def hourly_scrape(business_id, link, business_name):
    try:
        print(f"[HOURLY] Scraping Trustpilot for business: {business_name} ({link})")
        scrape_trustpilot_reviews(link, business_id=business_id, business_name=business_name)
    except Exception as e:
        print(f"Error in hourly scrape for business {business_name}: {e}")
    # Reschedule next hourly scrape if still enabled
    business = business_collection.find_one({'_id': business_id})
    if business:
        trustpilot_settings = business.get("settings", {}).get("reviewPlatforms", {}).get("trustpilot", {})
        enabled = trustpilot_settings.get("enabled", False)
        if enabled:
            hourly_scheduler.enter(3600, 1, hourly_scrape, (business_id, link, business_name))
        else:
            if business_id in hourly_scrape_business_ids:
                hourly_scrape_business_ids.remove(business_id)

def start_schedulers():
    main_scheduler.enter(0, 1, periodic_scrape)
    threading.Thread(target=main_scheduler.run, daemon=True).start()
    threading.Thread(target=hourly_scheduler.run, daemon=True).start()
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down schedulers.")

def get_reviews_from_page(url):
    try:
        req = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        req.raise_for_status()  # Raise an error for bad status codes
        time.sleep(2)  # Add a delay to avoid overwhelming the server
        soup = BeautifulSoup(req.text, 'html.parser')
        reviews_raw = soup.find("script", id="__NEXT_DATA__").string
        reviews_raw = json.loads(reviews_raw)
        return reviews_raw["props"]["pageProps"]["reviews"]
    except (requests.RequestException, json.JSONDecodeError, AttributeError) as e:
        return []

def scrape_trustpilot_reviews(base_url: str, business_id=None, business_name=None):
    reviews_data = []
    page_number = 1
    while True:
        url = f"{base_url}?page={page_number}"
        reviews = get_reviews_from_page(url)
        if not reviews:
            break
        for review in reviews:
            data = {
                'id_review': review["id"],
                'caption': review["text"],
                'relative_date': review.get("dates", {}).get("publishedDateRelative", ""),
                'retrieval_date': datetime.utcnow(),
                'rating': review["rating"],
                'username': review["consumer"]["displayName"],
                'n_review_user': review["consumer"].get("numberOfReviews", 0),
                'url_user': review["consumer"].get("profileUrl", ""),
                'business_id': str(business_id) if business_id else "",
                'business_name': business_name if business_name else "",
                'business_slug': "",  # Set as needed
                'business_url': base_url,  # Trustpilot business reviews page
                'scraped_at': datetime.utcnow(),
                'review_url': f"https://au.trustpilot.com/reviews/{review['id']}",  # Trustpilot review link
                'source': "Trustpilot"
            }
            # Insert into MongoDB
            collection.update_one(
                {"id_review": data["id_review"]},
                {"$set": data},
                upsert=True
            )
            reviews_data.append(data)
        page_number += 1
    # Remove duplicates based on the 'id_review' field
    seen = set()
    unique_reviews = []
    for d in reviews_data:
        if d['id_review'] not in seen:
            unique_reviews.append(d)
            seen.add(d['id_review'])
    return unique_reviews

def process_all_businesses():
    all_businesses = business_collection.find({})
    for business in all_businesses:
        try:
            trustpilot_settings = business.get("settings", {}).get("reviewPlatforms", {}).get("trustpilot", {})
            enabled = trustpilot_settings.get("enabled", False)
            link = trustpilot_settings.get("link", None)
            if enabled and link:
                print(f"Scraping Trustpilot for business: {business.get('businessName', business.get('_id'))} ({link})")
                reviews = scrape_trustpilot_reviews(link, business_id=business.get('_id'), business_name=business.get('businessName'))
                print(f"Scraped {len(reviews)} reviews for {business.get('businessName', business.get('_id'))}")
            else:
                print(f"Skipping business: {business.get('businessName', business.get('_id'))} (Trustpilot not enabled or link missing)")
        except Exception as e:
            print(f"Error processing business {business.get('businessName', business.get('_id'))}: {e}")

if __name__ == "__main__":
    start_schedulers()
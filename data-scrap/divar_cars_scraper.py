import os
from pathlib import Path
import requests
import json
import urllib.parse
import csv
import re
import logging
import time
from datetime import datetime
import random
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type




logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("data-scrap\\scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)




SEARCH_URL = "https://api.divar.ir/v8/postlist/w/search"

PRELOADED_STATE_PATTERN = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", re.DOTALL)


BASE_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

SEARCH_HEADERS = {
    **BASE_HTML_HEADERS,
    "User-Agent": USER_AGENTS[0],
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json",
}

session = requests.Session()


FIELD_KEYWORDS = {
    "mileage": "کارکرد",
    "year": ["سال تولید", "مدل"],
    "color": "رنگ",
    "brand_model": "برند و مدل",
    "gearbox": "گیربکس",
    "fuel_type": "سوخت",
    "base_price": "قیمت پایه",
    "engine_score": "موتور",
    "chassis_score": "شاسی",
    "body_score": "بدنه",
}


# 1:teh, 4:isf, 6:shz, 25:bu
CITY_IDS = ["1",]  #  "1", "4", "6", "25"


CATEGORY_SLUG = "light"  # light cars
CATEGORY_URL_SLUG = "car"

TARGETED_QUERY = "ساینا"  # e.g. "ساینا", None for no target


DATA_DIR = Path("data-scrap") / "data"
# DEBUG_DIR = DATA_DIR / "debug"
SEARCH_RESULTS_PATH = DATA_DIR / "search_results.csv"
DETAILS_SUCCESS_PATH = DATA_DIR / "details_success.csv"
PENDING_TOKENS_PATH = DATA_DIR / "failed_tokens.csv"


MAX_SEARCH_PAGES = 6
MAX_RETRY_ROUNDS = 6

MIN_DELAY = 2.0
MAX_DELAY = 4.5
DETAIL_MIN_DELAY = 6.0
DETAIL_MAX_DELAY = 12.0

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 6.0




def polite_sleep(min_s=MIN_DELAY, max_s=MAX_DELAY):
    time.sleep(random.uniform(min_s, max_s))

def get_random_headers():
    return {**BASE_HTML_HEADERS, "User-Agent": random.choice(USER_AGENTS)}




def persian_to_english_digits(text: str) -> str:
    if not text:
        return text
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    english_digits = "0123456789"
    trans_table = str.maketrans(
        persian_digits + arabic_digits,
        english_digits + english_digits,
    )
    return text.translate(trans_table)

def parse_price(price_text: str):
    if not price_text:
        return None, "missing"
    if "توافق" in price_text:
        return None, "negotiable"
    normalized = persian_to_english_digits(price_text)
    digits_only = re.sub(r"[^\d]", "", normalized)
    if not digits_only:
        return None, "unparseable"
    return int(digits_only), "ok"

def parse_mileage(mileage_text: str):
    if not mileage_text:
        return None
    normalized = persian_to_english_digits(mileage_text)
    digits_only = re.sub(r"[^\d]", "", normalized)
    if not digits_only:
        return None
    return int(digits_only)




def extract_post_fields(widget: dict) -> dict:
    data = widget.get("data", {})
    action_log = widget.get("action_log", {})
    server_info = action_log.get("server_side_info", {}).get("info", {})

    price_text = data.get("middle_description_text", "")
    price_value, price_status = parse_price(price_text)

    mileage_text = data.get("top_description_text", "")
    mileage_value = parse_mileage(mileage_text)

    web_info = data.get("action", {}).get("payload", {}).get("web_info", {})

    return {
        "token": data.get("token"),
        "title": data.get("title"),
        "price_text_raw": price_text,
        "price_toman": price_value,
        "price_status": price_status,
        "mileage_text_raw": mileage_text,
        "mileage_km": mileage_value,
        "city_persian": web_info.get("city_persian"),
        "district_persian": web_info.get("district_persian"),
        "posted_time_text": data.get("bottom_description_text"),
        "sort_date": server_info.get("sort_date"),
        "image_count": data.get("image_count"),
        "scraped_at": datetime.now().isoformat(),
    }


def extract_post_detail_fields(post: dict) -> dict:
    webengage = post.get("webengage", {})
    sections = post.get("sections", [])

    token = webengage.get("token") or post.get("token")
    web_url = post.get("share", {}).get("web_url")

    title = None
    posted_time_and_location = None

    for wtype, wdata in _iter_section_widgets(sections, "TITLE"):
        if wtype == "LEGEND_TITLE_ROW":
            found = wdata.get("title")
            if found:
                title = title or found
        elif wtype == "EXPANDABLE_SECTION":
            found = wdata.get("title")
            if found:
                posted_time_and_location = posted_time_and_location or found

    title = title or post.get("title") or webengage.get("title")
    posted_time_text = posted_time_and_location
    location_text = (
        post.get("breadcrumb_text")
        or webengage.get("district")
        and f"{webengage.get('city', '')}، {webengage.get('district', '')}".strip("، ")
    )

    if not title:
        try:
            available_sections = (
                list(sections.keys()) if isinstance(sections, dict)
                else [s.get("section_name") for s in sections]
            )
        except Exception:
            available_sections = "(could not enumerate sections)"
        title = f"[NO_TITLE_FOUND - sections were: {available_sections}]"

    mileage_text = year_text = color = None
    brand_model_text = gearbox = fuel_type = base_price_text = None
    engine_score = chassis_front_score = chassis_rear_score = body_score = gearbox_score = None

    for wtype, wdata in _iter_section_widgets(sections, "LIST_DATA"):
        if wtype == "GROUP_INFO_ROW":
            for item in wdata.get("items", []):
                item_title = item.get("title", "")
                value = item.get("value", "")
                if FIELD_KEYWORDS["mileage"] in item_title:
                    mileage_text = value
                elif any(kw in item_title for kw in FIELD_KEYWORDS["year"]):
                    year_text = value
                elif FIELD_KEYWORDS["color"] in item_title:
                    color = value

        elif wtype == "UNEXPANDABLE_ROW":
            row_title = wdata.get("title", "")
            value = wdata.get("value", "")
            if FIELD_KEYWORDS["brand_model"] in row_title:
                brand_model_text = value
            elif FIELD_KEYWORDS["gearbox"] in row_title:
                gearbox = value
            elif FIELD_KEYWORDS["fuel_type"] in row_title:
                fuel_type = value
            elif FIELD_KEYWORDS["base_price"] in row_title:
                base_price_text = value

        elif wtype == "SCORE_ROW":
            row_title = wdata.get("title", "")
            score = wdata.get("descriptive_score", "")
            if FIELD_KEYWORDS["engine_score"] in row_title:
                engine_score = score
            elif "شاسی جلو" in row_title:
                chassis_front_score = score
            elif "شاسی عقب" in row_title:
                chassis_rear_score = score
            elif FIELD_KEYWORDS["chassis_score"] in row_title:
                chassis_front_score = score
                chassis_rear_score = score
            elif FIELD_KEYWORDS["body_score"] in row_title:
                body_score = score
            elif FIELD_KEYWORDS["gearbox"] in row_title:
                gearbox_score = score

    description_text = None
    for wtype, wdata in _iter_section_widgets(sections, "DESCRIPTION"):
        if wtype == "DESCRIPTION_ROW":
            description_text = wdata.get("text")
            break

    base_price_value, _ = parse_price(base_price_text) if base_price_text else (None, None)

    return {
        "token": token,
        "web_url": web_url,
        "title": title,
        "posted_time_text": posted_time_text,
        "location": location_text,
        "mileage_text_raw": mileage_text,
        "mileage_km": parse_mileage(mileage_text) if mileage_text else None,
        "year_text": year_text,
        "color": color,
        "brand_model_text": brand_model_text,
        "gearbox": gearbox,
        "fuel_type": fuel_type,
        "base_price_text_raw": base_price_text,
        "base_price_toman": base_price_value,
        "engine_condition": engine_score,
        "chassis_front_condition": chassis_front_score,
        "chassis_rear_condition": chassis_rear_score,
        "body_condition": body_score,
        "gearbox_condition": gearbox_score,
        "description": description_text,
        "scraped_at": datetime.now().isoformat(),
    }


class DetailFetchError(Exception):
    pass




def build_detail_url(token: str, title: str = "x") -> str:
    clean_title = re.sub(r'[^\w\s-]', '', title.strip()) if title else ""
    clean_title = re.sub(r'[\s/]+', '-', clean_title)
    slug = urllib.parse.quote(clean_title) if clean_title else "x"
    return f"https://divar.ir/v/{slug}/{token}"


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=RETRY_BACKOFF_BASE, min=RETRY_BACKOFF_BASE),
    retry=retry_if_exception_type((requests.RequestException, DetailFetchError, json.JSONDecodeError)),
    reraise=True,
)
def fetch_post_detail(token: str, title: str = "x") -> dict:
    url = build_detail_url(token, title)
    logger.info(f"  -> {url}")

    resp = session.get(url, headers=get_random_headers(), timeout=15)
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        wait_s = float(retry_after) if retry_after else RETRY_BACKOFF_BASE * 5
        logger.warning(f"    429 rate-limited on token {token}, waiting {wait_s:.0f}s")
        time.sleep(wait_s)
        raise DetailFetchError(f"Rate limited (429) for token {token}")
    resp.raise_for_status()

    match = PRELOADED_STATE_PATTERN.search(resp.text)
    if not match:
        raise DetailFetchError(
            f"__PRELOADED_STATE__ not found - page length {len(resp.text)} chars, "
            f"status {resp.status_code}. Likely a bot-check/redirect page."
        )

    state = json.loads(match.group(1))
    post = state.get("currentPost", {}).get("post", {})

    # has_usable_data = bool(post.get("token")) or bool(post.get("webengage", {}).get("token"))
    # if not has_usable_data:
    #     os.makedirs(DEBUG_DIR, exist_ok=True)
    #     debug_path = os.path.join(DEBUG_DIR, f"debug_empty_{token}.json")
    #     try:
    #         with open(debug_path, "w", encoding="utf-8") as f:
    #             json.dump(post, f, ensure_ascii=False, indent=2)
    #     except OSError:
    #         debug_path = "(could not write debug file)"
    #     raise DetailFetchError(
    #         f"post has no usable data - raw content dumped to {debug_path}."
    #     )
    has_usable_data = bool(post.get("token")) or bool(post.get("webengage", {}).get("token"))
    if not has_usable_data:
        raise DetailFetchError(f"post has no usable data for token {token}.")

    returned_token = post.get("webengage", {}).get("token") or post.get("token")
    if returned_token and returned_token != token:
        raise DetailFetchError(f"Token mismatch: requested {token}, page contained {returned_token}.")

    return post


def _iter_section_widgets(sections, section_name: str):
    if isinstance(sections, dict):
        for widget in sections.get(section_name, []):
            wtype = widget.get("widgetType")
            wdata = widget.get("dto", {}).get("data", {})
            yield wtype, wdata
    else:
        for s in sections:
            if s.get("section_name") == section_name:
                for widget in s.get("widgets", []):
                    yield widget.get("widget_type"), widget.get("data", {})


def fetch_all_post_details(records: list, delay: bool = True) -> list:
    details = []
    failures = []

    for i, record in enumerate(records, 1):
        if i > 1 and (i - 1) % 10 == 0:
            long_pause = random.uniform(30, 60)
            logger.info(f"  Pausing {long_pause:.0f}s after {i - 1} requests to avoid rate-limiting...")
            time.sleep(long_pause)
        token = record.get("token")
        title = record.get("title", "x")

        if not token:
            logger.warning(f"[{i}/{len(records)}] Skipping record with no token: {record}")
            failures.append({"index": i, "token": None, "reason": "no token in record"})
            continue

        logger.info(f"[{i}/{len(records)}] Fetching detail for token {token} ({title!r})")
        try:
            detail_json = fetch_post_detail(token, title)
            extracted = extract_post_detail_fields(detail_json)
            # if extracted.get("title", "").startswith("[NO_TITLE_FOUND"):
            #     os.makedirs(DEBUG_DIR, exist_ok=True)
            #     with open(os.path.join(DEBUG_DIR, f"debug_notitle_{token}.json"), "w", encoding="utf-8") as f:
            #         json.dump(detail_json, f, ensure_ascii=False, indent=2)
            #     logger.warning(f"    OK but NO_TITLE - dumped to debug_notitle_{token}.json")
            # else:
            #     logger.info(f"    OK")
            if extracted.get("title", "").startswith("[NO_TITLE_FOUND"):
                logger.warning(f"    OK but title not found for token {token}")
            else:
                logger.info(f"    OK")
            details.append(extracted)
        except DetailFetchError as e:
            logger.error(f"    FAILED: {e}")
            failures.append({"index": i, "token": token, "reason": str(e)})
        except Exception as e:
            logger.error(f"    UNEXPECTED ERROR: {type(e).__name__}: {e}")
            failures.append({"index": i, "token": token, "reason": f"{type(e).__name__}: {e}"})

        if delay:
            polite_sleep(DETAIL_MIN_DELAY, DETAIL_MAX_DELAY)

    if failures:
        logger.warning(f"{len(failures)} of {len(records)} detail fetches failed:")
        for f in failures:
            logger.warning(f"  [{f['index']}] token={f['token']}: {f['reason']}")

    failed_records = [
        r for r in records
        if r.get("token") in {f["token"] for f in failures}
    ]
    return details, failed_records


def fetch_all_post_details_until_done(records: list, max_rounds: int = 5) -> list:
    all_details = []
    remaining = records

    for round_num in range(1, max_rounds + 1):
        if not remaining:
            break

        logger.info(f"=== Round {round_num}/{max_rounds}: fetching {len(remaining)} records ===")
        details, failed = fetch_all_post_details(remaining)
        all_details.extend(details)

        remaining = failed
        if remaining and round_num < max_rounds:
            cooldown = random.uniform(45, 90)
            logger.info(f"  {len(remaining)} still failing - cooling down {cooldown:.0f}s before next round...")
            time.sleep(cooldown)

    if remaining:
        logger.warning(f"{len(remaining)} records still failed after {max_rounds} rounds - saving to pending file.")
        pending = [
            {
                "token": r.get("token"),
                "title": r.get("title"),
                "fail_count": max_rounds,
                "last_attempt": datetime.now().isoformat(),
            }
            for r in remaining
        ]
        save_pending_tokens(pending, PENDING_TOKENS_PATH)
    else:
        save_pending_tokens([], PENDING_TOKENS_PATH)

    return all_details




def build_search_payload(pagination_state: dict = None, query: str = None) -> dict:
    payload = {
        "city_ids": CITY_IDS,
        "disable_recommendation": False,
        "map_state": {
            "camera_info": {"bbox": {}},
        },
        "search_data": {
            "form_data": {
                "data": {
                    "category": {
                        "str": {"value": CATEGORY_SLUG}
                    }
                }
            }
        },
    }
    if query:
        payload["search_data"]["query"] = query

    if pagination_state:
        payload["pagination_data"] = {
            "@type": "type.googleapis.com/post_list.PaginationData",
            "last_post_date": pagination_state["last_post_date"],
            "page": pagination_state["page"],
            "layer_page": pagination_state["page"],
            "cumulative_widgets_count": pagination_state["cumulative_widgets_count"],
            "pelle_layer_id": -1,
            "pelle_max_score": -1,
            "filters_hash": pagination_state["filters_hash"],
            "search_uid": pagination_state["search_uid"],
            "search_bookmark_info": pagination_state["search_bookmark_info"],
            "viewed_tokens": pagination_state["viewed_tokens"],
        }
        payload["search_data"]["server_payload"] = {
            "@type": "type.googleapis.com/widgets.SearchData.ServerPayload",
            "additional_form_data": {
                "data": {
                    "sort": {"str": {"value": "sort_date"}}
                }
            }
        }

    return payload


def fetch_search_page(pagination_state: dict = None, query: str = None) -> dict:
    payload = build_search_payload(pagination_state, query)
    
    resp = session.post(SEARCH_URL, headers=SEARCH_HEADERS, json=payload, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        wait_s = float(retry_after) if retry_after else 30.0
        logger.warning(f"  429 rate-limited on search page, waiting {wait_s:.0f}s")
        time.sleep(wait_s)
    resp.raise_for_status()

    return resp.json()


def build_next_pagination_state(result: dict, current_state: dict = None) -> dict:
    pdata = result.get("pagination", {}).get("data", {})
    return {
        "last_post_date": pdata.get("last_post_date"),
        "page": pdata.get("page"),
        "cumulative_widgets_count": pdata.get("cumulative_widgets_count"),
        "filters_hash": pdata.get("filters_hash"),
        "search_uid": pdata.get("search_uid"),
        "search_bookmark_info": pdata.get("search_bookmark_info"),
        "viewed_tokens": pdata.get("viewed_tokens"),
    }


def scrape_all_pages(max_pages: int = 10, query: str = None):
    all_records = []
    pagination_state = None

    for page_num in range(1, max_pages + 1):
        logger.info(f"Fetching page {page_num}...")
        try:
            result = fetch_search_page(pagination_state, query)
        except requests.RequestException as e:
            logger.error(f"  Error: {e}")
            break

        widgets = result.get("list_widgets", [])
        post_widgets = [w for w in widgets if w.get("widget_type") == "POST_ROW"]

        if not post_widgets:
            logger.info("  No more listings returned.")
            break

        for w in post_widgets:
            all_records.append(extract_post_fields(w))

        logger.info(f"  Got {len(post_widgets)} listings. Total so far: {len(all_records)}")

        pagination = result.get("pagination", {})
        if not pagination.get("has_next_page"):
            logger.info("  Reached the last page.")
            break

        pagination_state = build_next_pagination_state(result, pagination_state)
        polite_sleep()

    return all_records




def append_to_csv(records: list, filename: str):
    if not records:
        return
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    file_exists = os.path.isfile(filename)
    keys = []
    for r in records:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with open(filename, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        if not file_exists:
            writer.writeheader()
        writer.writerows(records)
    logger.info(f"Appended {len(records)} records to {filename}")


def load_pending_tokens(filename: str) -> list:
    if not os.path.isfile(filename):
        return []
    with open(filename, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def save_pending_tokens(records: list, filename: str):
    if not records:
        if os.path.isfile(filename):
            os.remove(filename)  # nothing pending anymore
        return
    keys = ["token", "title", "fail_count", "last_attempt"]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(records)


def load_existing_tokens(filename) -> set:
    if not os.path.isfile(filename):
        return set()
    with open(filename, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return {row["token"] for row in reader if row.get("token")}


def query_result_paths(query: str) -> tuple:
    safe_query = re.sub(r'[^\w\-]+', '_', query.strip())
    search_path = DATA_DIR / f"query_{safe_query}_results.csv"
    details_path = DATA_DIR / f"query_{safe_query}_details.csv"
    return search_path, details_path




if __name__ == "__main__":
    if TARGETED_QUERY:
        search_path, details_path = query_result_paths(TARGETED_QUERY)
    else:
        search_path, details_path = SEARCH_RESULTS_PATH, DETAILS_SUCCESS_PATH

    records = scrape_all_pages(max_pages=MAX_SEARCH_PAGES, query=TARGETED_QUERY)

    existing_search_tokens = load_existing_tokens(search_path)
    new_records = [r for r in records if r.get("token") not in existing_search_tokens]
    append_to_csv(new_records, search_path)

    already_done = load_existing_tokens(details_path)
    records_with_tokens = [r for r in records if r.get("token") and r["token"] not in already_done]
    details = fetch_all_post_details_until_done(records_with_tokens, max_rounds=MAX_RETRY_ROUNDS)

    append_to_csv(details, details_path)

#MadMad_645
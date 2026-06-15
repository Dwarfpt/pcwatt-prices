#!/usr/bin/env python3
"""
PCWatt bulk price collector for Moldovan PC-hardware shops.

Walks the category listings of supported stores, extracts product names and
prices from the HTML cards, and writes them into the CSV file that the PCWatt
app picks up on startup (D:\\hardware_prices.csv by default).

Unlike scratch/hardware_scraper.py (one product page per run), this collects
hundreds of items per run.

Usage:
    python price_collector.py                          # everything, 3 pages per category
    python price_collector.py --sites darwin,xstore    # selected stores
    python price_collector.py --categories cpu,gpu     # selected categories
    python price_collector.py --pages 1 --delay 2      # lighter/politer run
    python price_collector.py --output my_prices.csv

Be polite: keep --delay >= 1 second; this script is for personal use to feed
your own PCWatt price database.
"""
import argparse
import csv
import datetime
import html
import io
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ro,ru;q=0.9,en;q=0.8',
}

PRICE_RE = re.compile(r'(\d[\d\s., ]*)\s*(lei|mdl|€|eur|zł|pln|\$|usd|руб|₽)?', re.I)

# Lazily-started headless browser for JS-rendered stores (Playwright). Only
# created when a 'render': 'playwright' site is actually scraped, so the
# collector still runs without Playwright installed for every other store.
_PW = {'ctx': None, 'browser': None}


def _render_html(url, wait_ms=2500):
    if _PW['browser'] is None:
        from playwright.sync_api import sync_playwright
        _PW['ctx'] = sync_playwright().start()
        _PW['browser'] = _PW['ctx'].chromium.launch(headless=True)
    page = _PW['browser'].new_page(user_agent=HEADERS['User-Agent'])
    try:
        page.goto(url, wait_until='networkidle', timeout=45000)
        page.wait_for_timeout(wait_ms)
        return page.content()
    finally:
        page.close()


def _close_browser():
    if _PW['browser'] is not None:
        try:
            _PW['browser'].close()
            _PW['ctx'].stop()
        except Exception:
            pass
        _PW['browser'] = None

# Static EUR rates for cross-store comparison only (display keeps originals)
EUR_RATES = {'MDL': 20.0, 'EUR': 1.0, 'PLN': 4.3, 'USD': 1.08, 'RUB': 100.0,
             'RON': 5.0, 'BYN': 3.5, 'KZT': 500.0, 'BRL': 6.0}

# Store domain -> ISO country, so we can keep the cheapest offer PER COUNTRY
# (the app then shows the price for the user's region). Keep in sync with the
# app's lib/services/store_region.dart.
STORE_COUNTRY = {
    'darwin.md': 'MD', 'enter.online': 'MD', 'xstore.md': 'MD',
    'onliner.by': 'BY', 'proshop.de': 'DE', 'morele.net': 'PL',
    'shop.kz': 'KZ', 'regard.ru': 'RU', 'kabum.com.br': 'BR',
    'vexio.ro': 'RO', 'ultra.md': 'MD',
}


def country_of_store(store: str) -> str:
    """Best-effort ISO country for a store string; falls back to its ccTLD."""
    s = (store or '').lower()
    for domain, code in STORE_COUNTRY.items():
        if domain in s:
            return code
    m = re.search(r'\.([a-z]{2})$', s)
    return m.group(1).upper() if m else (s or '??')

# ─── Site profiles ───────────────────────────────────────────────────────────
# parser: which extraction strategy to use for a product card
#   'gtm'    — JSON in the data-gtm attribute (enter.online)
#   'darwin' — .price-new + longest link text
#   'xstore' — <a> name + span.fw-bold price
SITES = {
    'darwin': {
        'base': 'https://darwin.md',
        'card': 'div.product-card.product-item',
        'parser': 'darwin',
        'currency': 'MDL',
        'page_param': 'page',
        'categories': {
            'cpu': '/componente-pc/procesoare',
            'gpu': '/componente-pc/placi-video',
            'motherboard': '/componente-pc/motherboard',
            'ram': '/componente-pc/ram',
            'storage': '/componente-pc/dispozitive-de-stocare/ssd',
            'psu': '/componente-pc/power-supply',
            'cooler': '/componente-pc/coolere',
            'case': '/componente-pc/carcase',
            'paste': '/accesorii/pasta-termoconductoare',
        },
    },
    'enter': {
        'base': 'https://enter.online',
        'card': 'div.product-item.product-card',
        'parser': 'gtm',
        'currency': 'MDL',
        'page_param': 'page',
        'categories': {
            'cpu': '/calculatoare/processor',
            'gpu': '/calculatoare/videocard',
            'motherboard': '/calculatoare/mainboard',
            'ram': '/calculatoare/memorii-ram',
            'storage': '/calculatoare/dispozitive-de-stocare/ssd',
            'psu': '/calculatoare/power-supply',
            'cooler': '/calculatoare/cooler',
            'case': '/calculatoare/carcase',
            'paste': '/accesorii/pasta-termoconductoare',
        },
    },
    'xstore': {
        'base': 'https://xstore.md',
        'card': 'div.cardd',
        'parser': 'xstore',
        'currency': 'MDL',
        'page_param': 'page',
        'categories': {
            'cpu': '/componente-pc/procesoare',
            'gpu': '/componente-pc/placi-video',
            'motherboard': '/componente-pc/placi-de-baza',
            'ram': '/componente-pc/memorii-ram',
            'case': '/componente-pc/carcase',
        },
    },
    # ─── Moldova: JS-rendered SPA, needs Playwright (pip install playwright
    #     && playwright install chromium). Cards: .product-card ───
    'ultra': {
        'base': 'https://ultra.md',
        'render': 'playwright',
        'card': '.product-card',
        'parser': 'ultra',
        'currency': 'MDL',
        'page_param': 'page',
        'categories': {
            'cpu': '/componente-pc/procesoare',
            'gpu': '/componente-pc/placi-video',
            'motherboard': '/componente-pc/placi-de-baza',
            'ram': '/componente-pc/memorie-ram',
            'storage': '/componente-pc/unitate-ssd',
            'psu': '/componente-pc/surse-de-alimentare',
            'cooler': '/componente-pc/coolere-procesor',
            'case': '/componente-pc/carcase-pc',
            'fan': '/componente-pc/ventilatoare-pc',
        },
    },
    # ─── Romania: inline ecListedProducts JSON-ish object per card ───
    'vexio': {
        'base': 'https://www.vexio.ro',
        'card': 'article.product-box',
        'parser': 'vexio',
        'currency': 'RON',
        'page_param': 'page',
        'categories': {
            'cpu': '/procesoare/',
            'gpu': '/placi-video/',
            'motherboard': '/placi-de-baza/',
            'ram': '/memorii-ram/',
            'storage': '/ssd-uri/',
            'psu': '/surse/',
            'cooler': '/coolere-si-ventilatoare/',
            'case': '/carcase-pc/',
        },
    },
    'proshop': {
        'base': 'https://www.proshop.de',
        'card': 'li.site-productlist-item',
        'parser': 'proshop',
        'currency': 'EUR',
        'page_param': 'pn',
        'categories': {
            'cpu': '/CPU',
            'gpu': '/Grafikkarte',
            'motherboard': '/Motherboard',
            'ram': '/RAM',
            'storage': '/SSD',
            'psu': '/Netzteil',
            'cooler': '/CPU-Kuehler',
            'case': '/Gehaeuse',
        },
    },
    'morele': {
        'base': 'https://www.morele.net',
        'card': 'div.cat-product.card',
        'parser': 'morele',
        'currency': 'PLN',
        'page_style': 'morele',  # pagination: <url>,,,,,,,,0,,,,,s/<n>/
        'categories': {
            'cpu': '/kategoria/procesory-45/',
            'gpu': '/kategoria/karty-graficzne-12/',
            'motherboard': '/kategoria/plyty-glowne-42/',
            'ram': '/kategoria/pamieci-ram-38/',
            'storage': '/kategoria/dyski-ssd-518/',
            'psu': '/kategoria/zasilacze-komputerowe-33/',
            'cooler': '/kategoria/chlodzenie-cpu-633/',
            'case': '/kategoria/obudowy-32/',
            'paste': '/kategoria/pasty-termoprzewodzace-1292/',
            'pad': '/kategoria/termopady-12300/',
        },
    },
    # ─── Belarus: open JSON catalog API ───
    'onliner': {
        'base': 'https://catalog.onliner.by',
        'api': 'onliner',
        'currency': 'BYN',
        'categories': {
            'cpu': 'cpu',
            'gpu': 'videocard',
            'motherboard': 'motherboard',
            'ram': 'dram',
            'storage': 'ssd',
            'psu': 'powersupply',
            'case': 'chassis',
            'fan': 'fan',
        },
    },
    # ─── Kazakhstan: Bitrix shop with data-product JSON on each card ───
    'shopkz': {
        'base': 'https://shop.kz',
        'card': 'div.bx_catalog_item',
        'parser': 'shopkz',
        'currency': 'KZT',
        'page_param': 'PAGEN_1',
        'categories': {
            'cpu': '/protsessory/',
            'gpu': '/videokarty/',
            'motherboard': '/materinskie-platy/',
            'ram': '/operativnaya-pamyat/',
            'storage': '/ssd-diski/',
            'psu': '/bloki-pitaniya/',
            'case': '/korpusa/',
            'paste': '/offers/termopasta/',
        },
    },
    # ─── Russia: Next.js page payload (title/price/id triplets) ───
    'regard': {
        'base': 'https://www.regard.ru',
        'page_parser': 'regard',
        'currency': 'RUB',
        'delay': 8,  # aggressive rate limiting (429 after a few quick requests)
        'page_style': 'regard',  # /catalog/group2000.htm -> /catalog/group2000/page2.htm
        'categories': {
            'cpu': '/catalog/group2000.htm',
            'gpu': '/catalog/group1013.htm',
            'motherboard': '/catalog/group1000.htm',
            'ram': '/catalog/group1010.htm',
            'storage': '/catalog/group1015.htm',
            'psu': '/catalog/group1225.htm',
            'cooler': '/catalog/group5162.htm',
            'case': '/catalog/group1032.htm',
        },
    },
    # ─── Brazil: public catalog API ───
    'kabum': {
        'base': 'https://www.kabum.com.br',
        'api': 'kabum',
        'currency': 'BRL',
        'categories': {
            'cpu': 'processadores',
            'gpu': 'placa-de-video-vga',
            'motherboard': 'placas-mae',
            'ram': 'memoria-ram',
            'storage': 'ssd-2-5',
            'psu': 'fontes',
            'cooler': 'coolers',
        },
    },
}

# Category words that stores prepend to product names; stripping them makes
# the table readable and the names language-neutral.
NAME_PREFIXES = re.compile(
    r'^(procesor|processor|процессор|placa video|placă video|видеокарта|karta graficzna|'
    r'placa de baza|placă de bază|материнская плата|płyta główna|'
    r'memorie ram|memorie|оперативная память|pamięć ram|pamięć|'
    r'sursa de alimentare|sursă de alimentare|блок питания|zasilacz|'
    r'carcasa|carcasă|корпус|obudowa|cooler|кулер|chłodzenie|dysk ssd|dysk|накопитель|ssd|'
    r'past[ăay] termoconductoare|pasta termoprzewodząca|pasta termoprzewodzaca|'
    r'термопаста|термопрокладка|pad termiczny|termopad)\s+',
    re.I)


def clean_name(name: str) -> str:
    name = html.unescape(name or '')  # decode &trade; &reg; &amp; etc.
    name = re.sub(r'[™®©]', ' ', name)  # drop TM / (R) / (C)
    name = re.sub(r'\s+', ' ', name).strip()
    # darwin.md appends promo labels to the card link text
    name = re.sub(r'\s*Cashback\s+\d+\s*lei\s*$', '', name, flags=re.I)
    # shop.kz appends serial numbers
    name = re.sub(r'\s*\(SN:[^)]*\)\s*$', '', name, flags=re.I)
    name = NAME_PREFIXES.sub('', name).strip()
    return name


def parse_price(text: str):
    """'12 869 MDL' / '2.699,00 lei' -> (12869.0, 'MDL')"""
    if not text:
        return None, None
    text = text.replace(' ', ' ')
    m = PRICE_RE.search(text)
    if not m or not m.group(1):
        return None, None
    raw = m.group(1).replace(' ', '')
    cur_token = (m.group(2) or 'MDL').lower()
    currency = {'lei': 'MDL', 'mdl': 'MDL', '€': 'EUR', 'eur': 'EUR',
                'zł': 'PLN', 'pln': 'PLN',
                '$': 'USD', 'usd': 'USD', 'руб': 'RUB', '₽': 'RUB'}.get(cur_token, 'MDL')
    if '.' in raw and ',' in raw:
        if raw.find('.') < raw.find(','):
            raw = raw.replace('.', '').replace(',', '.')
        else:
            raw = raw.replace(',', '')
    elif ',' in raw:
        parts = raw.split(',')
        raw = raw.replace(',', '.') if len(parts[-1]) == 2 else raw.replace(',', '')
    elif raw.count('.') > 1:
        raw = raw.replace('.', '')
    try:
        value = float(raw)
    except ValueError:
        return None, None
    return (value, currency) if value > 0 else (None, None)


def absolutize(url, base):
    if not url:
        return ''
    if url.startswith('http'):
        return url
    return base + (url if url.startswith('/') else '/' + url)


def extract_card(card, profile):
    """Returns (name, price, currency, url) or None."""
    parser = profile['parser']
    base = profile['base']

    if parser == 'gtm':
        raw = card.get('data-gtm')
        if not raw:
            return None
        try:
            data = json.loads(raw)
            item = data['ecommerce']['items'][0]
            name = clean_name(item['item_name'])
            price = float(item['price'])
            currency = data['ecommerce'].get('currency', profile['currency'])
            link = card.find('a', href=True)
            url = absolutize(link['href'] if link else '', base)
            if name and price > 0:
                return name, price, currency, url
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        return None

    if parser == 'xstore':
        link = card.find('a', href=True)
        price_el = card.find('span', class_='fw-bold')
        if not link or not price_el:
            return None
        name = clean_name(link.get_text(' ', strip=True))
        price, currency = parse_price(price_el.get_text(' ', strip=True))
        if name and price:
            return name, price, currency or profile['currency'], absolutize(link['href'], base)
        return None

    if parser == 'ultra':
        t = card.select_one('.product-card__title')
        p = card.select_one('.product-card__current-price')
        a = card.select_one('a.product-card__link') or card.find('a', href=True)
        if t and p:
            name = clean_name(t.get_text(' ', strip=True))
            price, currency = parse_price(p.get_text(' ', strip=True))
            if name and price:
                return name, price, currency or 'MDL', \
                    absolutize(a['href'] if a else '', base)
        return None

    if parser == 'vexio':
        # Each card embeds an ecListedProducts JS object with clean fields.
        for sc in card.find_all('script'):
            t = sc.string or ''
            if 'item_name' not in t:
                continue
            nm = re.search(r"item_name:\s*'([^']*)'", t)
            pm = re.search(r"\bprice:\s*([\d.]+)", t)
            um = re.search(r"\burl:\s*'([^']*)'", t)
            cm = re.search(r"currency:\s*'([^']*)'", t)
            if nm and pm:
                name = clean_name(nm.group(1))
                try:
                    price = float(pm.group(1))
                except ValueError:
                    return None
                if name and price > 0:
                    return name, price, (cm.group(1) if cm else 'RON'), \
                        (um.group(1) if um else '')
        # Fallback: visible title + price.
        link = card.select_one('h2 a') or card.find('a', href=True)
        price_el = card.select_one('.price')
        if link and price_el:
            name = clean_name(link.get('title') or link.get_text(' ', strip=True))
            price, currency = parse_price(price_el.get_text(' ', strip=True))
            if name and price:
                return name, price, currency or 'RON', absolutize(link['href'], base)
        return None

    if parser == 'darwin':
        price_el = card.select_one('.price-new')
        if not price_el:
            return None
        price, currency = parse_price(price_el.get_text(' ', strip=True))
        if not price:
            return None
        # The product title is the longest link text inside the card
        best, best_href = '', ''
        for a in card.find_all('a', href=True):
            text = a.get('title') or a.get_text(' ', strip=True)
            if text and len(text) > len(best):
                best, best_href = text, a['href']
        name = clean_name(best)
        if len(name) >= 8:
            return name, price, currency or profile['currency'], absolutize(best_href, base)
        return None

    if parser == 'proshop':
        link = card.select_one('a.site-product-link')
        title = card.select_one('h2[product-display-name], a.site-product-link h2')
        if not link or not title:
            return None
        name = clean_name(title.get_text(' ', strip=True))
        m = re.search(r'(\d[\d.,]*)\s*€|€\s*(\d[\d.,]*)', card.get_text(' ', strip=True))
        if not m:
            return None
        price, _ = parse_price((m.group(1) or m.group(2)) + ' €')
        if name and price:
            return name, price, 'EUR', absolutize(link['href'], base)
        return None

    if parser == 'morele':
        name = clean_name(card.get('data-product-name') or '')
        try:
            price = float(card.get('data-product-price') or 0)
        except ValueError:
            return None
        link_el = card.select_one('[data-link-href-param]')
        url = absolutize(link_el['data-link-href-param'] if link_el else '', base)
        if name and price > 0:
            return name, price, 'PLN', url
        return None

    if parser == 'shopkz':
        raw = card.get('data-product')
        if not raw:
            return None
        try:
            d = json.loads(raw)
            name = clean_name(d.get('item_name') or '')
            price = float(d.get('price') or 0)
        except (ValueError, TypeError):
            return None
        link = card.find('link', attrs={'itemprop': 'url'}) or card.find('a', href=True)
        url = absolutize(link.get('href') if link else '', base)
        if name and price > 0:
            return name, price, 'KZT', url
        return None

    return None


def parse_regard_page(html, base):
    """regard.ru is Next.js: the full product list sits in __NEXT_DATA__."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []

    rows = []

    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, dict):
            title, price, pid = node.get('title'), node.get('price'), node.get('id')
            if (isinstance(title, str) and len(title) > 4 and
                    isinstance(price, (int, float)) and price > 0 and
                    isinstance(pid, int) and 'grpid' in node):
                rows.append((clean_name(title), float(price), 'RUB', f'{base}/product/{pid}'))
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(data)
    return rows


def fetch_api_page(session, profile, slug, page):
    """Returns list of (name, price, currency, url) for API-backed stores."""
    api = profile['api']
    if api == 'onliner':
        url = f'https://catalog.api.onliner.by/search/{slug}?page={page}'
        r = session.get(url, headers={**HEADERS, 'Accept': 'application/json'}, timeout=25)
        if r.status_code != 200:
            return None, url
        out = []
        for p in r.json().get('products', []):
            price_min = (p.get('prices') or {}).get('price_min') or {}
            try:
                price = float(price_min.get('amount') or 0)
            except (ValueError, TypeError):
                continue
            name = clean_name(p.get('full_name') or '')
            if name and price > 0:
                out.append((name, price, 'BYN', p.get('html_url') or ''))
        return out, url

    if api == 'kabum':
        url = ('https://servicespub.prod.api.aws.grupokabum.com.br/catalog/v2/'
               f'products-by-category/hardware/{slug}?page_number={page}&page_size=50')
        r = session.get(url, headers={**HEADERS, 'Accept': 'application/json'}, timeout=25)
        if r.status_code != 200:
            return None, url
        out = []
        for item in r.json().get('data', []):
            attrs = item.get('attributes', {})
            try:
                price = float(attrs.get('price') or 0)
            except (ValueError, TypeError):
                continue
            name = clean_name(attrs.get('title') or '')
            if name and price > 0:
                out.append((name, price, 'BRL', f"{profile['base']}/produto/{item.get('id', '')}"))
        return out, url

    return None, ''


def collect_category(session, site_key, profile, cat_key, path, max_pages, delay):
    rows = []
    seen_names = set()
    store = profile['base'].split('//')[1].replace('www.', '').replace('catalog.', '')

    for page in range(1, max_pages + 1):
        # ── API-backed stores (onliner.by, kabum) ──
        if profile.get('api'):
            try:
                extracted_list, url = fetch_api_page(session, profile, path, page)
            except requests.RequestException as e:
                print(f'  [WARN] {site_key}/{cat_key} p{page}: {type(e).__name__}')
                break
            if extracted_list is None:
                print(f'  [WARN] {url}: bad response' + (' (check category slug)' if page == 1 else ''))
                break
        elif profile.get('render') == 'playwright':
            # ── JS-rendered stores (ultra.md) via headless Chromium ──
            url = profile['base'] + path
            if page > 1:
                sep = '&' if '?' in url else '?'
                url = f"{url}{sep}{profile['page_param']}={page}"
            try:
                html = _render_html(url)
            except Exception as e:
                print(f'  [WARN] {url}: render {type(e).__name__}'
                      ' (need: pip install playwright && playwright install chromium)')
                break
            soup = BeautifulSoup(html, 'html.parser')
            extracted_list = [e for e in
                              (extract_card(c, profile) for c in soup.select(profile['card']))
                              if e]
        else:
            # ── HTML-backed stores ──
            url = profile['base'] + path
            if page > 1:
                if profile.get('page_style') == 'morele':
                    url = f"{url},,,,,,,,0,,,,,s/{page}/"
                elif profile.get('page_style') == 'regard':
                    url = url.replace('.htm', f'/page{page}.htm')
                else:
                    sep = '&' if '?' in url else '?'
                    url = f"{url}{sep}{profile['page_param']}={page}"
            try:
                resp = session.get(url, headers=HEADERS, timeout=25)
                if resp.status_code == 429:
                    print(f'  [429] {url}: backing off 30s...')
                    time.sleep(30)
                    resp = session.get(url, headers=HEADERS, timeout=25)
            except requests.RequestException as e:
                print(f'  [WARN] {url}: {type(e).__name__}')
                break
            if resp.status_code != 200:
                print(f'  [WARN] {url}: HTTP {resp.status_code}' + (' (check category slug)' if page == 1 else ''))
                break

            if profile.get('page_parser') == 'regard':
                extracted_list = parse_regard_page(resp.text, profile['base'])
            else:
                soup = BeautifulSoup(resp.text, 'html.parser')
                extracted_list = [e for e in
                                  (extract_card(c, profile) for c in soup.select(profile['card']))
                                  if e]

        page_rows = []
        for extracted in extracted_list:
            if extracted[0] not in seen_names:
                seen_names.add(extracted[0])
                page_rows.append((*extracted, store, cat_key))
        print(f'  {site_key}/{cat_key} p{page}: {len(page_rows)} items ({url})')
        if not page_rows:
            break  # empty or repeated page — stop paginating
        rows.extend(page_rows)
        time.sleep(max(delay, profile.get('delay', 0)))
    return rows


def main():
    ap = argparse.ArgumentParser(description='PCWatt bulk price collector (Moldova)')
    ap.add_argument('--sites', default=','.join(SITES), help='comma list: ' + ','.join(SITES))
    ap.add_argument('--categories', default='', help='comma list: cpu,gpu,motherboard,ram,storage,psu,cooler,case')
    ap.add_argument('--pages', type=int, default=3, help='max listing pages per category (default 3)')
    ap.add_argument('--delay', type=float, default=1.5, help='seconds between requests (default 1.5)')
    ap.add_argument('--output', default='D:\\hardware_prices.csv', help='CSV path the PCWatt app reads')
    ap.add_argument('--history-dir', default='', help='folder for dated snapshots (default: price_history next to output)')
    ap.add_argument('--merge', action='store_true',
                    help='keep rows of stores NOT scraped this run from the existing output CSV')
    args = ap.parse_args()

    wanted_sites = [s.strip() for s in args.sites.split(',') if s.strip() in SITES]
    wanted_cats = {c.strip() for c in args.categories.split(',') if c.strip()}

    session = requests.Session()
    all_rows = []
    for site_key in wanted_sites:
        profile = SITES[site_key]
        print(f'== {site_key} ({profile["base"]}) ==')
        for cat_key, path in profile['categories'].items():
            if wanted_cats and cat_key not in wanted_cats:
                continue
            all_rows.extend(
                collect_category(session, site_key, profile, cat_key, path, args.pages, args.delay))
    _close_browser()  # tear down headless Chromium if any JS store was scraped

    # --merge: carry over rows of stores that were not scraped this run
    if args.merge and os.path.exists(args.output):
        scraped_stores = {SITES[s]['base'].split('//')[1].replace('www.', '').replace('catalog.', '')
                          for s in wanted_sites}
        with open(args.output, encoding='utf-8-sig', newline='') as f:
            for row in list(csv.reader(f))[1:]:
                if len(row) >= 6 and row[3] not in scraped_stores:
                    all_rows.append((row[0], float(row[1]), row[2], row[4], row[3], row[5]))
        print(f'[merge] carried over rows from stores not in this run')

    # Dedupe per (product, country): keep the cheapest offer per normalized
    # name WITHIN each country, comparing prices in EUR. This leaves one row
    # per country per product so the app can show a price for the user's
    # region (a user in MD must not be shown a Belarus price).
    def eur(price, currency):
        return price / EUR_RATES.get(currency, 1.0)

    best = {}
    for name, price, currency, url, store, category in all_rows:
        key = (re.sub(r'\s+', ' ', name).lower(), country_of_store(store))
        if key not in best or eur(price, currency) < eur(best[key][1], best[key][2]):
            best[key] = (name, price, currency, url, store, category)

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ordered = sorted(best.values(), key=lambda r: (r[5], r[0].lower()))
    rows = [[name, f'{price:.2f}', currency, store, url, category, now]
            for name, price, currency, url, store, category in ordered]
    header = ['component_name', 'price', 'currency', 'store', 'url', 'category', 'scraped_at']

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    # utf-8-sig (BOM) so Excel/Notepad open the file with correct Cyrillic
    # and diacritics; the app strips the BOM when reading.
    with open(args.output, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

    # Daily snapshot for price history (graphs, "price dropped" badges later)
    history_dir = args.history_dir or os.path.join(out_dir, 'price_history')
    os.makedirs(history_dir, exist_ok=True)
    snapshot = os.path.join(history_dir, f'prices_{datetime.date.today().isoformat()}.csv')
    with open(snapshot, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

    # Compact per-product price history the app downloads to draw sparklines.
    # Shape: { "normalized name": [[YYYY-MM-DD, eur], ...] }, last 60 points.
    history_json = os.path.join(out_dir, 'price_history.json')
    today = datetime.date.today().isoformat()
    try:
        with open(history_json, encoding='utf-8') as f:
            hist = json.load(f)
    except (FileNotFoundError, ValueError):
        hist = {}
    for name, price, currency, _url, _store, _cat in ordered:
        key = re.sub(r'\s+', ' ', name).lower()
        eur_price = round(eur(price, currency), 2)
        points = hist.get(key, [])
        if points and points[-1][0] == today:
            points[-1] = [today, eur_price]  # overwrite same-day re-run
        else:
            points.append([today, eur_price])
        hist[key] = points[-60:]  # keep ~2 months
    with open(history_json, 'w', encoding='utf-8') as f:
        json.dump(hist, f, ensure_ascii=False, separators=(',', ':'))

    # Human-friendly README for the prices repo (GitHub also renders the CSV
    # itself as a searchable table).
    by_cat = {}
    by_store = {}
    for _, price, currency, _, store, category in ordered:
        by_cat[category] = by_cat.get(category, 0) + 1
        by_store[store] = by_store.get(store, 0) + 1
    readme = os.path.join(out_dir, 'README.md')
    with open(readme, 'w', encoding='utf-8') as f:
        f.write('# PCWatt Price Feed\n\n')
        f.write(f'Updated: **{now}** — **{len(ordered)}** items '
                f'(cheapest offer per product across stores).\n\n')
        f.write('Open [hardware_prices.csv](hardware_prices.csv) — GitHub renders it '
                'as a searchable table. Dated snapshots live in '
                '[price_history/](price_history/).\n\n')
        f.write('| Category | Items |\n|---|---:|\n')
        for cat in sorted(by_cat):
            f.write(f'| {cat} | {by_cat[cat]} |\n')
        f.write('\n| Store | Items (cheapest) |\n|---|---:|\n')
        for store in sorted(by_store):
            f.write(f'| {store} | {by_store[store]} |\n')

    print(f'\n[DONE] {len(best)} unique items ({len(all_rows)} scraped)')
    print(f'  feed:    {args.output}')
    print(f'  history: {snapshot}')
    print(f'  readme:  {readme}')


if __name__ == '__main__':
    main()


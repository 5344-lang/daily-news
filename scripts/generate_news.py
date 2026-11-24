import os
import re
import json
import time
import shutil
import smtplib
import urllib.parse
from datetime import datetime, date, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
import requests
from bs4 import BeautifulSoup
import trafilatura

try:
    import googlenewsdecoder
except Exception:
    googlenewsdecoder = None

try:
    from icalendar import Calendar
    import recurring_ical_events
except Exception:
    Calendar = None
    recurring_ical_events = None

KST = timezone(timedelta(hours=9))
NOW_KST = datetime.now(KST)
TODAY = NOW_KST.strftime("%Y-%m-%d")
TODAY_KR = NOW_KST.strftime("%Y년 %m월 %d일")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GCAL_ICS_URL = os.environ.get("GCAL_ICS_URL", "").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
TO_EMAIL = os.environ.get("TO_EMAIL", GMAIL_USER).strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S928N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
}
REQ_TIMEOUT = (5, 8)

KR_CITY_COORDS = {
    "서울": (37.5665, 126.9780),
    "대구": (35.8714, 128.6014),
    "경산": (35.8250, 128.7415),
    "부산": (35.1796, 129.0756),
    "인천": (37.4563, 126.7052),
    "광주": (35.1595, 126.8526),
    "대전": (36.3504, 127.3845),
    "울산": (35.5384, 129.3114),
    "제주": (33.4996, 126.5312),
}

WMO_DESC = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "가벼운 이슬비", 53: "이슬비", 55: "강한 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "우박 동반 뇌우", 99: "강한 뇌우",
}


def fetch_feed_safe(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
        res.raise_for_status()
        return feedparser.parse(res.content)
    except Exception as e:
        print(f"RSS 요청 건너뜀 ({url[:55]}): {e}")
        return feedparser.FeedParserDict(entries=[])


def fetch_weather(city_name):
    city = (city_name or "경산").strip()
    lat, lon = KR_CITY_COORDS.get(city, (None, None))
    if lat is None:
        try:
            g_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=ko&format=json"
            g_res = requests.get(g_url, timeout=REQ_TIMEOUT)
            results = g_res.json().get("results", [])
            if results:
                lat = results[0]["latitude"]
                lon = results[0]["longitude"]
        except Exception as e:
            print(f"도시 좌표 조회 실패 ({city}): {e}")
    if lat is None:
        city, (lat, lon) = "경산", KR_CITY_COORDS["경산"]

    try:
        w_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&timezone=Asia%2FSeoul&forecast_days=1"
        )
        res = requests.get(w_url, timeout=REQ_TIMEOUT)
        res.raise_for_status()
        daily = res.json().get("daily", {})
        code = daily.get("weathercode", [0])[0]
        t_min = round(daily.get("temperature_2m_min", [0])[0])
        t_max = round(daily.get("temperature_2m_max", [0])[0])
        rain = daily.get("precipitation_probability_max", [0])[0]
        desc = WMO_DESC.get(code, "흐림")
        weather_str = f"{city} {desc} {t_min}°/{t_max}° (강수 {rain}%)"
        print(f"날씨 수집 완료: {weather_str}")
        return weather_str
    except Exception as e:
        print(f"날씨 조회 건너뜀: {e}")
        return f"{city} 날씨 정보 없음"


def fetch_google_calendar_today():
    if not GCAL_ICS_URL:
        print("GCAL_ICS_URL 미등록 — 일정 건너뜀")
        return ""
    if Calendar is None or recurring_ical_events is None:
        print("icalendar 라이브러리 없음 — 일정 건너뜀")
        return ""
    try:
        res = requests.get(GCAL_ICS_URL, headers=HEADERS, timeout=(5, 10))
        res.raise_for_status()
        cal = Calendar.from_ical(res.content)
        start_of_day = datetime(NOW_KST.year, NOW_KST.month, NOW_KST.day, 0, 0, 0, tzinfo=KST)
        end_of_day = datetime(NOW_KST.year, NOW_KST.month, NOW_KST.day, 23, 59, 59, tzinfo=KST)
        events = recurring_ical_events.of(cal).between(start_of_day, end_of_day)

        items = []
        for ev in events:
            summary = str(ev.get("SUMMARY", "")).strip()
            if not summary:
                continue
            dtstart = ev.get("DTSTART").dt
            if isinstance(dtstart, datetime):
                dt_kst = dtstart.astimezone(KST) if dtstart.tzinfo else dtstart.replace(tzinfo=KST)
                sort_key = dt_kst.strftime("%H:%M")
                label = f"{sort_key} {summary}"
            elif isinstance(dtstart, date):
                sort_key = "00:00"
                label = f"종일 {summary}"
            else:
                continue
            items.append((sort_key, label))

        items.sort(key=lambda x: x[0])
        if not items:
            return "오늘 등록된 일정 없음"
        text = " · ".join(label for _, label in items[:4])
        if len(items) > 4:
            text += f" 외 {len(items) - 4}건"
        print(f"구글 캘린더 일정 수집 완료: {text}")
        return text
    except Exception as e:
        print(f"구글 캘린더 조회 건너뜀: {e}")
        return "일정 불러오기 실패"


def get_candidate_models():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    stable_flash, stable_lite, stable_pro, preview_text = [], [], [], []
    excluded = ["tts", "audio", "embedding", "omni", "robotics", "computer-use", "banana", "imagen", "veo", "lyria", "image"]
    try:
        res = requests.get(url, timeout=REQ_TIMEOUT)
        res.raise_for_status()
        for m in res.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            lower = name.lower()
            if any(x in lower for x in excluded):
                continue
            is_preview = any(x in lower for x in ["preview", "exp"])
            if "flash" in lower and "lite" in lower:
                (preview_text if is_preview else stable_lite).append(name)
            elif "flash" in lower:
                (preview_text if is_preview else stable_flash).append(name)
            elif "pro" in lower:
                (preview_text if is_preview else stable_pro).append(name)

        for lst in (stable_flash, stable_lite, stable_pro, preview_text):
            lst.sort(reverse=True)
    except Exception as e:
        print(f"모델 목록 조회 중 경고: {e}")

    text_models = (stable_flash + stable_lite + stable_pro + preview_text)[:8]
    for fb in ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]:
        if fb not in text_models:
            text_models.append(fb)
    return text_models


def load_history():
    path = "data/published_history.json"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("research", [])
                    data.setdefault("blogs", [])
                    return data
        except Exception:
            pass
    return {"research": [], "blogs": []}


def save_history(history):
    os.makedirs("data", exist_ok=True)
    history["research"] = history.get("research", [])[-120:]
    history["blogs"] = history.get("blogs", [])[-120:]
    with open("data/published_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def reset_image_dir():
    img_dir = "data/images"
    if os.path.exists(img_dir):
        shutil.rmtree(img_dir)
    os.makedirs(img_dir, exist_ok=True)


def download_local_image(img_url, filename, referer=""):
    if not img_url or not img_url.startswith("http"):
        return ""
    try:
        headers = dict(MOBILE_HEADERS)
        if referer:
            headers["Referer"] = referer
        res = requests.get(img_url, headers=headers, timeout=REQ_TIMEOUT)
        if res.status_code == 200 and len(res.content) > 2000:
            path = f"data/images/{filename}"
            with open(path, "wb") as f:
                f.write(res.content)
            return f"./{path}"
    except Exception as e:
        print(f"이미지 다운로드 건너뜀 ({filename}): {e}")
    return ""


def decode_google_news_builtin(google_url):
    parsed = urllib.parse.urlparse(google_url)
    parts = parsed.path.split("/")
    if "articles" in parts:
        b64_str = parts[parts.index("articles") + 1]
    elif "read" in parts:
        b64_str = parts[parts.index("read") + 1]
    else:
        return None

    res = requests.get(f"https://news.google.com/articles/{b64_str}", headers=HEADERS, timeout=REQ_TIMEOUT)
    soup = BeautifulSoup(res.text, "html.parser")
    tag = soup.find(attrs={"data-n-a-sg": True, "data-n-a-ts": True})
    if not tag:
        return None

    sg = tag["data-n-a-sg"]
    ts = tag["data-n-a-ts"]
    req_inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        b64_str,
        int(ts),
        sg
    ], separators=(",", ":"))
    f_req = json.dumps([[["Fbv4je", req_inner, None, "generic"]]], separators=(",", ":"))

    post_res = requests.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": HEADERS["User-Agent"],
        },
        data={"f.req": f_req},
        timeout=REQ_TIMEOUT,
    )
    for line in post_res.text.splitlines():
        if "garturlres" in line:
            outer = json.loads(line)
            inner = json.loads(outer[0][2])
            if len(inner) > 1 and isinstance(inner[1], str) and inner[1].startswith("http"):
                return inner[1]
    return None


def resolve_real_url(google_url):
    if googlenewsdecoder is not None:
        for fn_name in ("new_decoderv1", "gnewsdecoder", "decoderv1"):
            fn = getattr(googlenewsdecoder, fn_name, None)
            if callable(fn):
                try:
                    decoded = fn(google_url)
                    if isinstance(decoded, dict) and decoded.get("status") and decoded.get("decoded_url"):
                        return decoded["decoded_url"]
                    if isinstance(decoded, str) and decoded.startswith("http"):
                        return decoded
                except Exception:
                    pass
    try:
        builtin = decode_google_news_builtin(google_url)
        if builtin:
            return builtin
    except Exception:
        pass
    try:
        r = requests.get(google_url, headers=HEADERS, timeout=REQ_TIMEOUT, allow_redirects=True)
        return r.url
    except Exception:
        return google_url


def clean_article_text(text, max_len=3600):
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        s = line.replace("\u200b", "").strip()
        if not s:
            continue
        if any(k in s for k in ["무단전재", "재배포 금지", "저작권자", "ⓒ", "Copyright", "제보하기", "구독하기"]):
            continue
        s = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", s).strip()
        if len(s) >= 12:
            lines.append(s)
    return "\n\n".join(lines)[:max_len]


def scrape_generic_article(url, max_len=3200):
    image_url = ""
    full_text = ""
    try:
        res = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
        res.encoding = res.apparent_encoding or "utf-8"
        html = res.text
        soup = BeautifulSoup(html, "html.parser")

        og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if og_img and og_img.get("content"):
            cand = og_img["content"].strip()
            if cand.startswith("http") and "googleusercontent.com" not in cand:
                image_url = cand

        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
        if extracted and len(extracted.strip()) > 120:
            full_text = clean_article_text(extracted, max_len)
        else:
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 25]
            full_text = clean_article_text("\n".join(paras[:15]), max_len)
    except Exception as e:
        print(f"본문 추출 건너뜀 ({url[:50]}): {e}")
    return image_url, full_text


def fetch_rss_news(categories):
    collected = []
    idx = 0
    for cat in categories:
        if not cat.get("query"):
            continue
        q = urllib.parse.quote(cat["query"])
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        feed = fetch_feed_safe(url)
        for entry in feed.entries[:2]:
            raw_link = entry.get("link", "")
            real_url = resolve_real_url(raw_link)
            img, body = scrape_generic_article(real_url)
            title = entry.get("title", "").split(" - ")[0].strip()
            collected.append({
                "id": idx,
                "category": cat["name"],
                "title": title,
                "url": real_url,
                "source": entry.get("source", {}).get("title", "뉴스"),
                "image": img,
                "full_article": body if body else f"{title} 관련 보도입니다. 자세한 내용은 하단 원문 출처에서 확인하실 수 있습니다.",
                "has_image": bool(img),
                "has_body": len(body) > 200,
            })
            idx += 1
    print(f"총 {len(collected)}건 뉴스 수집 완료")
    return collected


def fetch_research_candidates(research_feeds, published_urls):
    candidates = []
    for feed_cfg in research_feeds:
        if not feed_cfg.get("enabled", True) or not feed_cfg.get("url"):
            continue
        feed = fetch_feed_safe(feed_cfg["url"])
        for entry in feed.entries[:4]:
            link = entry.get("link", "").strip()
            if not link or link in published_urls:
                continue
            img, body = scrape_generic_article(link, max_len=2000)
            if not body:
                raw_sum = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
                body = raw_sum[:1800]
            if len(body) < 180:
                continue
            candidates.append({
                "source_name": feed_cfg.get("name", "심리학 연구"),
                "title": entry.get("title", "").strip(),
                "url": link,
                "image": img,
                "body": body,
            })
            if len(candidates) >= 2:
                break
        if len(candidates) >= 2:
            break
    print(f"최신 심리 실험 연구 후보 {len(candidates)}건 수집 완료")
    return candidates


def normalize_blog_rss(raw_url):
    u = raw_url.strip()
    if not u:
        return ""
    if "rss.blog.naver.com" in u:
        return u
    m = re.search(r"blog\.naver\.com/([a-zA-Z0-9_-]+)", u)
    if m:
        return f"https://rss.blog.naver.com/{m.group(1)}.xml"
    if not u.startswith("http") and re.match(r"^[a-zA-Z0-9_-]+$", u):
        return f"https://rss.blog.naver.com/{u}.xml"
    return u


def scrape_naver_blog_post(url, img_idx):
    clean_url = url.split("?")[0]
    m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", clean_url)
    if not m:
        img, body = scrape_generic_article(url, max_len=4000)
        local_img = download_local_image(img, f"blog_{img_idx}.jpg", referer=url)
        return local_img or img, body

    blog_id, log_no = m.group(1), m.group(2)
    mobile_url = f"https://m.blog.naver.com/{blog_id}/{log_no}"
    image_url = ""
    full_text = ""

    try:
        res = requests.get(mobile_url, headers=MOBILE_HEADERS, timeout=REQ_TIMEOUT)
        res.encoding = "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")

        container = soup.select_one(".se-main-container") or soup.select_one("#postViewArea") or soup.select_one(".post_ct")
        if container:
            paras = []
            se_paras = container.select(".se-text-paragraph")
            if se_paras:
                for p in se_paras:
                    t = p.get_text(" ", strip=True).replace("\u200b", "").strip()
                    if t:
                        paras.append(t)
                full_text = "\n\n".join(paras)[:4000]
            else:
                full_text = clean_article_text(container.get_text("\n", strip=True), max_len=4000)

            for img_tag in container.select("img"):
                src = img_tag.get("data-lazy-src") or img_tag.get("src") or ""
                if src.startswith("http") and "sticker" not in src and "emoticon" not in src and "profile" not in src:
                    src = re.sub(r"type=w\d+(_blur)?", "type=w966", src)
                    image_url = src
                    break

        if not image_url:
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                image_url = og["content"].strip()

        if not full_text:
            extracted = trafilatura.extract(res.text, include_comments=False, include_tables=False)
            full_text = clean_article_text(extracted, max_len=4000)
    except Exception as e:
        print(f"네이버 블로그 추출 오류 ({mobile_url}): {e}")

    local_img = download_local_image(image_url, f"blog_{img_idx}.jpg", referer=mobile_url)
    return local_img, full_text


def fetch_blog_articles(blogs_cfg, published_urls):
    picked = []
    img_idx = 0
    for b in blogs_cfg:
        if not b.get("enabled", True) or not b.get("url"):
            continue
        rss_url = normalize_blog_rss(b["url"])
        feed = fetch_feed_safe(rss_url)
        if not feed.entries:
            continue

        fresh_entries = [e for e in feed.entries[:8] if e.get("link", "").split("?")[0] not in published_urls]
        candidates = (fresh_entries if fresh_entries else feed.entries[:2])[:4]

        selected_post = None
        for entry in candidates:
            link = entry.get("link", "").split("?")[0]
            title = entry.get("title", "").strip()
            if "blog.naver.com" in link:
                img, body = scrape_naver_blog_post(link, img_idx)
            else:
                raw_img, body = scrape_generic_article(link, max_len=4000)
                img = download_local_image(raw_img, f"blog_{img_idx}.jpg", referer=link) or raw_img

            if len(body) >= 250:
                selected_post = {
                    "blog_name": b.get("name", "구독 블로그"),
                    "title": title,
                    "url": link,
                    "image": img,
                    "full_article": body,
                    "published": entry.get("published", "")[:16],
                }
                img_idx += 1
                break

        if not selected_post and candidates:
            entry = candidates[0]
            link = entry.get("link", "").split("?")[0]
            raw_sum = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text("\n", strip=True)
            selected_post = {
                "blog_name": b.get("name", "구독 블로그"),
                "title": entry.get("title", "").strip(),
                "url": link,
                "image": "",
                "full_article": clean_article_text(raw_sum, max_len=3500) or "원문 링크에서 전체 내용을 확인하세요.",
                "published": entry.get("published", "")[:16],
            }

        if selected_post:
            picked.append(selected_post)
            if len(picked) >= 3:
                break

    print(f"구독 블로그 아티클 {len(picked)}편 수집 완료")
    return picked


def call_gemini_editor(news_items, research_candidates, text_models):
    compact_news = [
        {
            "id": item["id"],
            "category": item["category"],
            "title": item["title"],
            "source": item["source"],
            "has_image": item["has_image"],
            "has_body": item["has_body"],
            "preview": item["full_article"][:140],
        }
        for item in news_items
    ]

    research_block = [
        {
            "idx": i,
            "source_name": r["source_name"],
            "title": r["title"],
            "body": r["body"][:1600],
        }
        for i, r in enumerate(research_candidates[:2])
    ]

    prompt = f"""
당신은 고전 신문 'DAILY NEWS'의 편집장 겸 심리학 과학 저널리스트입니다.
오늘({TODAY_KR}) 수집된 뉴스 후보와 최신 심리학 실험 논문 기사를 바탕으로 JSON으로만 응답하세요.

[요구사항]
1. edition_quote: 신문 제호 아래 들어갈 짧은 명언 또는 편집장 한마디
2. lead_id: 1면 톱기사로 가장 적합한 뉴스 id 1개 (has_body와 has_image가 true인 기사 우선)
3. lead_takeaway: 1면 톱기사 핵심 통찰 한 줄
4. article_ids: 나머지 뉴스 중 카테고리별로 골고루 4~5개의 id 선택 (lead_id 중복 금지, has_body 우선)
5. research_report: [심리학 실험 연구 후보]가 비어 있지 않다면, 그중 가장 흥미로운 최신 실험 연구 1편을 골라 한국어 심층 기사로 번역·재구성할 것. (후보가 비어 있으면 null)
   - selected_idx: 선택한 연구의 idx 번호
   - title: 호기심을 끄는 한국어 기사 제목
   - journal_info: 원문에서 언급된 학술지명/연구기관/저자 정보 (없으면 출처명)
   - full_article: 4개 문단(\\n\\n 구분) — ① 흥미로운 질문 ② 구체적인 실험 방법 ③ 의외의 실험 결과 ④ 일상에 주는 시사점
   - takeaway: 연구 핵심 한 줄 요약

[출력 JSON 스키마]
{{
  "edition_quote": "문장",
  "lead_id": 0,
  "lead_takeaway": "핵심 한 줄",
  "article_ids": [1, 3, 5, 7],
  "research_report": {{
    "selected_idx": 0,
    "title": "한국어 제목",
    "journal_info": "학술지 또는 연구기관 정보",
    "full_article": "문단1\\n\\n문단2\\n\\n문단3\\n\\n문단4",
    "takeaway": "핵심 한 줄"
  }}
}}

[뉴스 후보]
{json.dumps(compact_news, ensure_ascii=False)}

[심리학 실험 연구 후보]
{json.dumps(research_block, ensure_ascii=False)}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    for model_name in text_models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(2):
            try:
                res = requests.post(endpoint, json=payload, timeout=(5, 40))
                if res.status_code == 200:
                    print(f"사용된 텍스트 모델: {model_name}")
                    text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(text)
                if res.status_code in (503, 429) and attempt == 0:
                    print(f"[{model_name}] 일시 지연({res.status_code}) — 3초 후 재시도...")
                    time.sleep(3)
                    continue
                print(f"[{model_name}] 건너뜀 ({res.status_code})")
                break
            except Exception as e:
                print(f"[{model_name}] 요청 예외: {e}")
                break

    raise RuntimeError("사용 가능한 Gemini 텍스트 모델을 찾지 못했습니다.")


def send_gmail(edition, site_url):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and TO_EMAIL):
        print("Gmail 설정이 없어 메일 발송을 건너뜁니다.")
        return

    lead = edition["lead_story"]
    research = edition.get("research_report")
    blogs = edition.get("blog_articles", [])
    lead_preview = lead.get("full_article", "").split("\n\n")[0]

    items_html = "".join(
        f"<li style='margin-bottom:8px;'><b>[{a['category']}]</b> {a['title']}</li>"
        for a in edition["articles"][:5]
    )
    extra_html = ""
    if research:
        extra_html += f"<div style='background:#efe9dc;border:1px solid #111;padding:12px;margin-top:14px;'><b>🔬 최신 심리 실험 리포트</b><br>{research['title']}</div>"
    if blogs:
        b_titles = "<br>".join(f"• [{b['blog_name']}] {b['title']}" for b in blogs)
        extra_html += f"<div style='background:#f7f4ec;border:1px solid #111;padding:12px;margin-top:10px;'><b>✍️ 구독 블로그 아티클</b><br>{b_titles}</div>"

    html = f"""
    <div style="font-family: Georgia, serif; max-width: 560px; margin: 0 auto; padding: 20px; border: 2px solid #111; background: #faf7f0; color: #111;">
      <div style="text-align: center; border-bottom: 3px double #111; padding-bottom: 10px; margin-bottom: 16px;">
        <h1 style="margin: 0; letter-spacing: 3px;">{edition.get('paper_name', 'DAILY NEWS')}</h1>
        <p style="margin: 4px 0 0; font-size: 13px;">{TODAY_KR} · {edition.get('weather', '')}</p>
      </div>
      <p style="font-size: 12px; font-weight: bold; margin-bottom: 4px;">[1면 톱기사 · {lead['category']}]</p>
      <h2 style="margin-top: 0;">{lead['title']}</h2>
      <p style="line-height: 1.6;">{lead_preview}</p>
      <hr style="border: 0; border-top: 1px solid #111; margin: 18px 0;">
      <ul style="padding-left: 18px; line-height: 1.6;">{items_html}</ul>
      {extra_html}
      <div style="text-align: center; margin-top: 24px;">
        <a href="{site_url}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; font-weight: bold;">
          📰 오늘의 신문 전체 읽기
        </a>
      </div>
    </div>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[DAILY NEWS] {TODAY_KR} — {lead['title']}"
    msg["From"] = GMAIL_USER
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())
    print("Gmail 발송 완료!")


def main():
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)

    reset_image_dir()
    history = load_history()

    weather_str = fetch_weather(config.get("weather_city", "경산"))
    schedule_str = fetch_google_calendar_today()

    text_models = get_candidate_models()
    news = fetch_rss_news(config.get("categories", []))
    research_candidates = fetch_research_candidates(config.get("research_feeds", []), history["research"])
    blog_articles = fetch_blog_articles(config.get("blogs", []), history["blogs"])

    news_map = {item["id"]: item for item in news}
    ai_data = call_gemini_editor(news, research_candidates, text_models)

    lead_id = ai_data.get("lead_id", 0)
    lead_item = news_map.get(lead_id, news[0])
    lead_story = {
        "category": lead_item["category"],
        "title": lead_item["title"],
        "full_article": lead_item["full_article"],
        "takeaway": ai_data.get("lead_takeaway", ""),
        "source": lead_item["source"],
        "url": lead_item["url"],
        "image": lead_item["image"],
    }

    chosen_ids = [i for i in ai_data.get("article_ids", []) if i in news_map and i != lead_item["id"]]
    if len(chosen_ids) < 4:
        for item in news:
            if item["id"] != lead_item["id"] and item["id"] not in chosen_ids:
                chosen_ids.append(item["id"])
            if len(chosen_ids) >= 5:
                break

    articles = [
        {
            "category": news_map[i]["category"],
            "title": news_map[i]["title"],
            "full_article": news_map[i]["full_article"],
            "source": news_map[i]["source"],
            "url": news_map[i]["url"],
            "image": news_map[i]["image"],
        }
        for i in chosen_ids[:5]
    ]

    research_report = None
    ai_research = ai_data.get("research_report")
    if ai_research and research_candidates:
        sel = ai_research.get("selected_idx", 0)
        if not isinstance(sel, int) or sel < 0 or sel >= len(research_candidates):
            sel = 0
        src_r = research_candidates[sel]
        local_r_img = download_local_image(src_r["image"], "research_0.jpg", referer=src_r["url"])
        research_report = {
            "source_name": src_r["source_name"],
            "title": ai_research.get("title", src_r["title"]),
            "journal_info": ai_research.get("journal_info", src_r["source_name"]),
            "full_article": ai_research.get("full_article", ""),
            "takeaway": ai_research.get("takeaway", ""),
            "url": src_r["url"],
            "image": local_r_img or src_r["image"],
        }
        if src_r["url"] not in history["research"]:
            history["research"].append(src_r["url"])

    for b in blog_articles:
        if b["url"] not in history["blogs"]:
            history["blogs"].append(b["url"])

    save_history(history)

    edition = {
        "date": TODAY,
        "date_kr": TODAY_KR,
        "paper_name": config.get("paper_name", "DAILY NEWS"),
        "weather": weather_str,
        "schedule": schedule_str,
        "edition_quote": ai_data.get("edition_quote", ""),
        "lead_story": lead_story,
        "articles": articles,
        "research_report": research_report,
        "blog_articles": blog_articles,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/today.json", "w", encoding="utf-8") as f:
        json.dump(edition, f, ensure_ascii=False, indent=2)

    send_gmail(edition, config.get("site_url", ""))
    print(f"{TODAY} DAILY NEWS 생성 완료!")


if __name__ == "__main__":
    main()

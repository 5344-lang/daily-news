import os
import re
import json
import time
import calendar
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlparse, urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None


import base64
from urllib.parse import urlparse, unquote


from urllib.parse import unquote, urljoin

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None


BLOCKED_DECODE_HOSTS = (
    "news.google.com",
    "google.com",
    "www.google.com",
    "google-analytics.com",
    "www.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "googlesyndication.com",
    "doubleclick.net",
    "gstatic.com",
    "googleusercontent.com",
)


def is_valid_article_url(u):
    if not u or not u.startswith(("http://", "https://")):
        return False
    host = (urlparse(u).hostname or "").lower()
    if not host:
        return False
    return not any(host == b or host.endswith("." + b) for b in BLOCKED_DECODE_HOSTS)


def decode_google_news_url(url):
    if not url or "news.google.com" not in url:
        return url

    # 1차: googlenewsdecoder 패키지 시도
    if gnewsdecoder is not None:
        try:
            res = gnewsdecoder(url, interval=1)
            if isinstance(res, dict) and res.get("status") and res.get("decoded_url"):
                decoded = res["decoded_url"].strip()
                if is_valid_article_url(decoded):
                    return decoded
        except Exception as e:
            print(f"[안내] googlenewsdecoder 실패, 페이지 추적 전환: {e}")

    # 2차: 구글 뉴스 articles 페이지에서 실제 외부 언론사 링크만 선별
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        final_url = (resp.url or url).strip()
        if is_valid_article_url(final_url):
            return final_url

        html = resp.text or ""
        soup = BeautifulSoup(html, "html.parser")

        for el in soup.find_all(attrs={"data-n-au": True}):
            cand = (el.get("data-n-au") or "").strip()
            if is_valid_article_url(cand):
                return cand

        for a in soup.find_all("a", href=True):
            href = urljoin(final_url, a["href"].strip())
            if is_valid_article_url(href):
                return href

        unescaped = html.replace("\\/", "/").replace("\\u003d", "=").replace("\\u0026", "&")
        for match in re.findall(r"https?://[^\s\"'<>\\]+", unescaped):
            cand = unquote(match).strip().rstrip(".,);'\"")
            if is_valid_article_url(cand) and not cand.lower().endswith((".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")):
                return cand
    except Exception as e:
        print(f"[경고] 구글 뉴스 페이지 링크 추출 실패: {e}")

    return url
    
KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
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
IGNORE_IMAGE_PATTERNS = [
    "tistory_admin/static",
    "opengraph.png",
    "no_image",
    "noimage",
    "default_image",
    "og_default",
    "google_news",
    "news.google.com",
    "googleusercontent.com",
    "gstatic.com",
]


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_history():
    data = load_json("data/published_history.json", {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("research", [])
    data.setdefault("blogs", [])
    data.setdefault("research_idx", 0)
    return data


def clean_article_text(text, max_len=2500):
    if not text:
        return ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if len(line) >= 15]
    cleaned = "\n\n".join(lines)
    return cleaned[:max_len].strip()


def fetch_feed_safe(url):
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code in (429, 503):
                wait = 3 * (attempt + 1)
                print(f"[안내] RSS 일시 차단({resp.status_code}), {wait}초 후 재시도...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except Exception as e:
            if attempt == 2:
                print(f"[경고] RSS 불러오기 실패 ({url}): {e}")
            else:
                time.sleep(2)
    return feedparser.FeedParserDict(entries=[])


def is_ignored_image(img_url):
    if not img_url:
        return True
    low = img_url.lower()
    return any(p.lower() in low for p in IGNORE_IMAGE_PATTERNS)


def download_local_image(img_url, filename, referer=None):
    if not img_url or is_ignored_image(img_url):
        return ""
    try:
        req_headers = dict(HEADERS)
        if referer:
            req_headers["Referer"] = referer
        resp = requests.get(img_url, headers=req_headers, timeout=12, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if "image" not in ctype:
            return ""
        content = resp.content
        if len(content) < 2500 or len(content) > 5 * 1024 * 1024:
            return ""
        os.makedirs("data/images", exist_ok=True)
        out_path = os.path.join("data", "images", filename)
        with open(out_path, "wb") as f:
            f.write(content)
        return f"./data/images/{filename}"
    except Exception:
        return ""


def scrape_generic_article(url, max_len=3500):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        og_img = ""
        meta_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if meta_img and meta_img.get("content"):
            og_img = urljoin(resp.url, meta_img["content"].strip())
            if is_ignored_image(og_img):
                og_img = ""

        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
            tag.decompose()

        container = (
            soup.find("article")
            or soup.find("div", class_=re.compile(r"(article|entry-content|post-content|contents_style|tt_article_useless_p_margin)", re.I))
            or soup.body
        )
        if not container:
            return og_img, ""

        # og:image가 없을 경우 본문 영역 내 첫 번째 유효 이미지 탐색
        if not og_img:
            for img in container.find_all("img"):
                cand = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
                if not cand:
                    continue
                full_img = urljoin(resp.url, cand.strip())
                if not is_ignored_image(full_img):
                    og_img = full_img
                    break

        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all(["p", "div"]) if p.name == "p" or not p.find("p")]
        raw_text = "\n".join(paragraphs) if paragraphs else container.get_text("\n", strip=True)
        return og_img, clean_article_text(raw_text, max_len=max_len)
    except Exception:
        return "", ""


def scrape_naver_blog_post(url, img_idx=0):
    try:
        m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url)
        if m:
            blog_id, log_no = m.group(1), m.group(2)
            target = f"https://m.blog.naver.com/{blog_id}/{log_no}"
        else:
            target = url.replace("://blog.naver.com", "://m.blog.naver.com")

        resp = requests.get(target, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        body_el = (
            soup.select_one(".se-main-container")
            or soup.select_one("#postViewArea")
            or soup.select_one(".post_ct")
        )
        if not body_el:
            return "", ""

        img_url = ""
        for img in body_el.find_all("img"):
            cand = img.get("data-lazy-src") or img.get("src") or ""
            if cand and "postfiles.pstatic.net" in cand and not is_ignored_image(cand):
                img_url = cand
                break

        local_img = download_local_image(img_url, f"blog_{img_idx}.jpg", referer=target) if img_url else ""
        text = clean_article_text(body_el.get_text("\n", strip=True), max_len=4000)
        return local_img, text
    except Exception:
        return "", ""


def normalize_blog_rss(raw_url):
    u = (raw_url or "").strip()
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


def fetch_weather(city):
    if not city:
        return ""
    try:
        geo = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(city)}&count=1&language=ko&format=json",
            timeout=10,
        ).json()
        results = geo.get("results") or []
        if not results:
            return f"{city} (날씨 정보 없음)"
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        w = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&timezone=Asia%2FSeoul&forecast_days=1",
            timeout=10,
        ).json()
        daily = w.get("daily", {})
        code = (daily.get("weathercode") or [0])[0]
        t_min = round((daily.get("temperature_2m_min") or [0])[0])
        t_max = round((daily.get("temperature_2m_max") or [0])[0])
        rain = (daily.get("precipitation_probability_max") or [0])[0]
        desc = WMO_DESC.get(code, "흐림")
        return f"{city} {desc} {t_min}°/{t_max}° (강수 {rain}%)"
    except Exception as e:
        print(f"[경고] 날씨 조회 실패: {e}")
        return ""


def build_search_query(raw_query, global_excludes_str):
    q = (raw_query or "").strip()
    if not q:
        return ""

    # 사용자가 직접 OR이나 괄호를 쓴 고급 검색식이면 존중
    if " OR " in q or "(" in q:
        base_q = q
    else:
        tokens = [t.strip() for t in re.split(r"[,]", q) if t.strip()]
        positives = []
        negatives = []
        for tok in tokens:
            # 쉼표 없이 공백으로 -단어가 붙은 경우 분리
            parts = tok.split()
            pos_parts = [p for p in parts if not p.startswith("-")]
            neg_parts = [p for p in parts if p.startswith("-") and len(p) > 1]
            if pos_parts:
                phrase = " ".join(pos_parts)
                positives.append(f'"{phrase}"' if " " in phrase else phrase)
            negatives.extend(neg_parts)

        if not positives:
            return q
        base_q = " OR ".join(positives)
        if negatives:
            base_q += " " + " ".join(negatives)

    # 공통 제외 단어 추가 (Google News URL 길이 제한 고려해 최대 10개)
    excludes = [w.strip().lstrip("-") for w in re.split(r"[,\s]+", global_excludes_str or "") if w.strip()]
    added = []
    for ex in excludes[:10]:
        token = f"-{ex}"
        if token not in base_q:
            added.append(token)
    if added:
        base_q = f"{base_q} {' '.join(added)}"
    return base_q


def fetch_news_candidates(categories, global_excludes, disliked_urls):
    candidates = []
    seen_titles = set()
    for cat in categories:
        cat_name = cat.get("name", "뉴스").strip()
        raw_query = cat.get("query", "").strip()
        if not raw_query:
            continue
        query = build_search_query(raw_query, global_excludes)
        rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:2d&hl=ko&gl=KR&ceid=KR:ko"
        time.sleep(1.5)
        feed = fetch_feed_safe(rss_url)
        count = 0
        for entry in feed.entries[:8]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link or link in disliked_urls:
                continue
            norm_title = re.sub(r"\W+", "", title)[:25]
            if norm_title in seen_titles:
                continue
            seen_titles.add(norm_title)

            source = ""
            if hasattr(entry, "source") and entry.source:
                source = entry.source.get("title", "")
            elif " - " in title:
                title, source = title.rsplit(" - ", 1)

            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
            candidates.append({
                "id": len(candidates),
                "category": cat_name,
                "title": title.strip(),
                "source": source.strip() or "뉴스",
                "url": link,
                "snippet": summary[:400],
            })
            count += 1
            if count >= 4:
                break
    print(f"뉴스 후보 {len(candidates)}건 수집 완료")
    return candidates


def fetch_research_candidates(research_feeds, history):
    enabled_feeds = [f for f in research_feeds if f.get("enabled", True) and f.get("url")]
    if not enabled_feeds:
        return []

    published_urls = history.get("research", [])
    start_idx = history.get("research_idx", 0) % len(enabled_feeds)
    candidates = []

    for offset in range(len(enabled_feeds)):
        cur_idx = (start_idx + offset) % len(enabled_feeds)
        feed_cfg = enabled_feeds[cur_idx]
        feed = fetch_feed_safe(feed_cfg["url"])
        for entry in feed.entries[:5]:
            link = entry.get("link", "").strip()
            if not link or link in published_urls:
                continue
            img, body = scrape_generic_article(link, max_len=1600)
            if not body:
                raw_sum = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
                body = raw_sum[:1500]
            if len(body) < 180:
                continue
            local_img = download_local_image(img, "research_0.jpg", referer=link) if img else ""
            candidates.append({
                "source_name": feed_cfg.get("name", "심리학 연구"),
                "title": entry.get("title", "").strip(),
                "url": link,
                "image": local_img or img,
                "body": body,
            })
            history["research_idx"] = (cur_idx + 1) % len(enabled_feeds)
            break
        if candidates:
            break

    print(f"최신 심리 실험 연구 후보 {len(candidates)}건 수집 완료 (다음 피드 순번: {history.get('research_idx', 0)})")
    return candidates


def is_recent_entry(entry, hours=48):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return False
    entry_ts = calendar.timegm(parsed)
    return (time.time() - entry_ts) <= (hours * 3600)


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

        mode = b.get("mode", "unread")
        if mode == "new_only":
            candidates = [
                e for e in feed.entries[:10]
                if e.get("link", "").split("?")[0] not in published_urls and is_recent_entry(e, hours=48)
            ][:3]
        else:
            candidates = [
                e for e in feed.entries[:30]
                if e.get("link", "").split("?")[0] not in published_urls
            ][:4]

        if not candidates:
            print(f"구독 블로그 [{b.get('name', '')}] ({mode}) — 조건에 맞는 글 없음, 건너뜀")
            continue

        selected_post = None
        for entry in candidates:
            link = entry.get("link", "").split("?")[0]
            title = entry.get("title", "").strip()
            if "blog.naver.com" in link:
                img, body = scrape_naver_blog_post(link, img_idx)
            else:
                raw_img, body = scrape_generic_article(link, max_len=4000)
                if is_ignored_image(raw_img):
                    raw_img = ""
                img = download_local_image(raw_img, f"blog_{img_idx}.jpg", referer=link) or raw_img

            if len(body) >= 80:
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


def build_quote_instruction(quote_mode, custom_prompt):
    if quote_mode == "none":
        return "edition_quote 값은 반드시 빈 문자열(\"\")로 비워두세요."
    if quote_mode == "famous_quote":
        return "edition_quote에는 아침에 읽기 좋은 철학·문학·심리학 명언 1줄과 출처(인물명)를 한국어로 작성하세요."
    if quote_mode == "ai_cheer":
        return "edition_quote에는 오늘 하루를 기분 좋게 시작할 수 있는 AI 편집장의 따뜻하고 담백한 응원 1줄을 작성하세요."
    if quote_mode == "humor":
        return "edition_quote에는 아침에 피식 웃을 수 있는 센스 있고 짧은 유머나 개그 1줄을 한국어로 작성하세요."
    if quote_mode == "custom" and custom_prompt:
        return f"edition_quote는 다음 독자 요청에 맞춰 1~2문장 이내로 작성하세요: {custom_prompt}"
    return "edition_quote에는 오늘 선별된 주요 뉴스들의 흐름을 관통하는 신문 편집장의 통찰력 있는 한줄평을 작성하세요."


def generate_edition_with_gemini(config, news_candidates, research_candidates, prefs):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")

    client = genai.Client(api_key=api_key)
    article_count = int(config.get("article_count", 6))
    article_count = max(3, min(9, article_count))
    sub_count = article_count - 1

    likes_text = "\n".join([f"- [{x.get('category','')}] {x.get('title','')}" for x in prefs.get("likes", [])[-15:]]) or "없음"
    dislikes_text = "\n".join([f"- [{x.get('category','')}] {x.get('title','')}" for x in prefs.get("dislikes", [])[-15:]]) or "없음"
    quote_inst = build_quote_instruction(config.get("quote_mode", "news_comment"), config.get("quote_custom_prompt", ""))

    prompt = f"""당신은 개인 맞춤형 조간 신문의 수석 편집장입니다.
아래 수집된 뉴스 후보와 독자 취향 기록을 참고하여 오늘의 신문을 JSON으로 작성하세요.

[독자 취향 기록]
- 최근 '좋아요'한 기사 (비슷한 주제·깊이 우선 선별):
{likes_text}
- 최근 '싫어요'한 기사 (비슷한 낚시성·홍보성·관심 밖 주제 제외):
{dislikes_text}

[뉴스 후보 목록]
{json.dumps(news_candidates, ensure_ascii=False)}

[심리학 실험 연구 원문 (있으면 한국어 심층 리포트로 번역·재구성, 없으면 null)]
{json.dumps(research_candidates[0] if research_candidates else None, ensure_ascii=False)}

[작성 규칙]
1. 카테고리 다양성을 살려 총 {article_count}개의 기사를 선별하세요 (1면 톱기사 `lead_story` 1건 + 일반 기사 `articles` {sub_count}건). 후보가 부족하면 있는 만큼만 선별하세요.
2. 기사를 새로 창작하거나 길게 요약하지 마세요. 선별한 기사의 `id`와 `category`만 정확히 반환하세요.
3. `id`는 반드시 뉴스 후보 목록의 정수 `id`를 그대로 적으세요.
4. {quote_inst}
5. `research_report`는 연구 원문이 있을 때만 한국어 기사(3~4문단)와 `takeaway`(핵심 1줄), `journal_info`(출처/연구진 요약)를 작성하고, 없으면 null로 두세요.

반드시 아래 JSON 스키마로만 응답하세요:
{{
  "edition_quote": "문자열",
  "lead_story": {{
    "id": 0,
    "category": "카테고리명"
  }},
  "articles": [
    {{
      "id": 1,
      "category": "카테고리명"
    }}
  ],
  "research_report": {{
    "title": "한국어 헤드라인",
    "journal_info": "출처 및 연구 요약 정보",
    "full_article": "문단1\\n\\n문단2\\n\\n문단3",
    "takeaway": "핵심 시사점 한 줄"
  }}
}}
"""

            # 사용 가능한 Gemini 모델 목록을 자동으로 조회해 덜 붐비는 순으로 시도
    candidate_models = []
    try:
        for m in client.models.list():
            name = (m.name or "").replace("models/", "")
            if (
                name.startswith("gemini-")
                and not any(x in name for x in ["embedding", "imagen", "veo", "tts", "audio", "vision", "robotics", "computer-use"])
            ):
                candidate_models.append(name)
    except Exception as e:
        print(f"[경고] 모델 목록 자동 조회 실패: {e}")

    if not candidate_models:
        candidate_models = ["gemini-3.8-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

    def model_priority(name):
        if "flash-lite" in name:
            return (0, name)
        if "flash" in name:
            return (1, name)
        if "pro" in name:
            return (2, name)
        return (3, name)

    models_to_try = sorted(set(candidate_models), key=model_priority)[:6]
    print(f"[안내] 시도할 모델 순서: {models_to_try}")

    last_err = None
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                print(f"[안내] {model_name} 모델로 편집 요청 중... ({attempt + 1}/2)")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.4,
                    ),
                )
                return json.loads(response.text)
            except Exception as e:
                last_err = e
                err_str = str(e)
                if "404" in err_str or "NOT_FOUND" in err_str or "400" in err_str:
                    print(f"[안내] {model_name} 사용 불가, 다음 모델로 전환합니다.")
                    break
                if "503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str:
                    wait_sec = 6 * (attempt + 1)
                    print(f"[안내] {model_name} 서버 혼잡. {wait_sec}초 후 재시도...")
                    time.sleep(wait_sec)
                else:
                    raise
    raise last_err

def main():
    now_kst = datetime.now(KST)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    date_str = now_kst.strftime("%Y-%m-%d")
    date_kr = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일 ({weekdays[now_kst.weekday()]})"

    config = load_json("config.json", {})
    history = load_history()
    prefs = load_json("data/preferences.json", {"likes": [], "dislikes": [], "saved": []})
    disliked_urls = {x.get("url") for x in prefs.get("dislikes", []) if x.get("url")}

    show_weather = config.get("show_weather", True)
    weather_text = fetch_weather(config.get("weather_city", "대구")) if show_weather else ""

    news_candidates = fetch_news_candidates(
        config.get("categories", []),
        config.get("exclude_keywords", ""),
        disliked_urls,
    )
    research_candidates = fetch_research_candidates(config.get("research_feeds", []), history)
    blog_articles = fetch_blog_articles(config.get("blogs", []), history.get("blogs", []))

    ai_result = generate_edition_with_gemini(config, news_candidates, research_candidates, prefs)
    cand_map = {c["id"]: c for c in news_candidates}

    def enrich_article(item, img_name):
        if not item:
            return None
        src = cand_map.get(item.get("id"), {})
        raw_url = src.get("url", "")
        url = decode_google_news_url(raw_url)
        is_decoded = bool(url) and ("news.google.com" not in url)
        print(
            f"[디버그URL] {img_name} | 변환성공={is_decoded} | "
            f"원래URL={raw_url[:80]} | 반환URL={url[:120]}"
        )
        raw_img = ""
        scraped_body = ""
        if url:
            raw_img, scraped_body = scrape_generic_article(url, max_len=4000)
        local_img = download_local_image(raw_img, img_name, referer=url) if raw_img else ""
        final_img = local_img or raw_img
        final_body = scraped_body or (src.get("snippet") or "")
        본문출처 = "원문추출" if scraped_body else "RSS요약"
        print(f"[수집] {img_name} | {본문출처} | 사진={bool(final_img)} | 길이={len(final_body)} | URL={url[:60]}")
        return {
            "category": item.get("category") or src.get("category", "뉴스"),
            "title": src.get("title") or item.get("title", ""),
            "source": src.get("source", "뉴스"),
            "url": url,
            "image": final_img,
            "full_article": final_body,
            "takeaway": "",
        }

    lead_story = enrich_article(ai_result.get("lead_story"), "lead.jpg")
    articles = []
    for idx, art in enumerate(ai_result.get("articles") or []):
        enriched = enrich_article(art, f"art_{idx}.jpg")
        if enriched and enriched.get("title"):
            articles.append(enriched)

    research_report = None
    if research_candidates and ai_result.get("research_report"):
        rc = research_candidates[0]
        rr = ai_result["research_report"]
        research_report = {
            "source_name": rc["source_name"],
            "title": rr.get("title") or rc["title"],
            "journal_info": rr.get("journal_info", rc["source_name"]),
            "url": rc["url"],
            "image": rc.get("image", ""),
            "full_article": rr.get("full_article", ""),
            "takeaway": rr.get("takeaway", ""),
        }
        history["research"].append(rc["url"])
        history["research"] = history["research"][-200:]

    for b in blog_articles:
        if b.get("url"):
            history["blogs"].append(b["url"])
    history["blogs"] = history["blogs"][-300:]

    edition = {
        "paper_name": config.get("paper_name", "DAILY NEWS"),
        "date": date_str,
        "date_kr": date_kr,
        "show_weather": show_weather,
        "weather": weather_text,
        "edition_quote": ai_result.get("edition_quote", ""),
        "lead_story": lead_story,
        "articles": articles,
        "research_report": research_report,
        "blog_articles": blog_articles,
    }

    save_json("data/today.json", edition)
    save_json("data/published_history.json", history)
    print(f"[완료] {date_kr} 신문 발행 성공! (기사 {1 + len(articles)}건, 연구 {1 if research_report else 0}건, 블로그 {len(blog_articles)}건)")


if __name__ == "__main__":
    main()

import os
import re
import json
import time
import calendar
import base64
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlparse, urljoin, unquote

import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

try:
    import trafilatura
except ImportError:
    trafilatura = None

BLOCKED_DECODE_HOSTS = (
    "news.google.com", "google.com", "www.google.com",
    "google-analytics.com", "www.google-analytics.com",
    "googletagmanager.com", "www.googletagmanager.com",
    "googlesyndication.com", "doubleclick.net", "gstatic.com",
    "fonts.gstatic.com", "fonts.googleapis.com", "googleusercontent.com",
    "w3.org", "www.w3.org", "angular.dev", "www.angular.dev",
)
BLOCKED_PATH_KEYWORDS = ("/license", "/terms", "/privacy", "/policies", "/support", "/help")


def is_valid_article_url(url):
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not host or any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_DECODE_HOSTS):
        return False
    return not any(keyword in path for keyword in BLOCKED_PATH_KEYWORDS)


def _extract_url_from_cbmi_token(url):
    """Best-effort extraction for older Google News tokens embedding a destination URL."""
    try:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if "articles" not in parts:
            return ""
        index = parts.index("articles")
        if index + 1 >= len(parts):
            return ""
        token = parts[index + 1].split("?")[0]
        token += "=" * ((4 - len(token) % 4) % 4)
        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                decoded = decoder(token).decode("utf-8", errors="ignore").replace("\\/", "/")
                for match in re.findall(r"https?://[^\s\"'<>\\]+", decoded):
                    candidate = unquote(match).strip().rstrip(".,);'\"\\")
                    if is_valid_article_url(candidate):
                        return candidate
            except Exception:
                continue
    except Exception:
        pass
    return ""


def decode_google_news_builtin(url):
    """Resolve newer Google News article tokens through Google's batchexecute endpoint."""
    try:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if "articles" not in parts:
            return ""
        token = parts[parts.index("articles") + 1]
        response = requests.get(f"https://news.google.com/articles/{token}", headers=HEADERS, timeout=12)
        response.raise_for_status()
        tag = BeautifulSoup(response.text, "html.parser").find(attrs={"data-n-a-sg": True, "data-n-a-ts": True})
        if not tag:
            return ""
        request_inner = json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            token, int(tag["data-n-a-ts"]), tag["data-n-a-sg"],
        ], separators=(",", ":"))
        form_request = json.dumps([[["Fbv4je", request_inner, None, "generic"]]], separators=(",", ":"))
        result = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "User-Agent": HEADERS["User-Agent"]},
            data={"f.req": form_request}, timeout=12,
        )
        result.raise_for_status()
        for line in result.text.splitlines():
            if "garturlres" not in line:
                continue
            outer = json.loads(line)
            inner = json.loads(outer[0][2])
            if len(inner) > 1 and isinstance(inner[1], str) and is_valid_article_url(inner[1]):
                return inner[1]
    except Exception as exc:
        print(f"[안내] Google News batchexecute 해독 실패: {exc}")
    return ""


def decode_google_news_url(url):
    if not url or "news.google.com" not in url:
        return url
    if gnewsdecoder is not None:
        try:
            result = gnewsdecoder(url, interval=1)
            decoded = result.get("decoded_url") if isinstance(result, dict) else None
            if isinstance(decoded, str) and is_valid_article_url(decoded):
                return decoded.strip()
        except Exception as exc:
            print(f"[안내] googlenewsdecoder 실패: {exc}")
    for candidate in (_extract_url_from_cbmi_token(url), decode_google_news_builtin(url)):
        if is_valid_article_url(candidate):
            return candidate
    try:
        response = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if is_valid_article_url(response.url):
            return response.url
    except Exception:
        pass
    return url


KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
WMO_DESC = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "짙은 안개",
    51: "가벼운 이슬비", 53: "이슬비", 55: "강�� 이슬비", 61: "약한 비", 63: "비", 65: "강한 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "우박 동반 뇌우", 99: "강한 뇌우",
}
IGNORE_IMAGE_PATTERNS = [
    "tistory_admin/static", "opengraph.png", "no_image", "noimage", "default_image", "og_default",
    "google_news", "news.google.com", "googleusercontent.com", "gstatic.com",
]


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            pass
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_history():
    data = load_json("data/published_history.json", {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("research", [])
    data.setdefault("blogs", [])
    data.setdefault("research_idx", 0)
    return data


def clean_article_text(text, max_len=4000):
    if not text:
        return ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if len(line) >= 15]
    return "\n\n".join(dict.fromkeys(lines))[:max_len].strip()


def fetch_feed_safe(url):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code in (429, 503):
                wait = 3 * (attempt + 1)
                print(f"[안내] RSS 일시 차단({response.status_code}), {wait}초 후 재시도...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return feedparser.parse(response.content)
        except Exception as exc:
            if attempt == 2:
                print(f"[경고] RSS 불러오기 실패 ({url}): {exc}")
            else:
                time.sleep(2)
    return feedparser.FeedParserDict(entries=[])


def is_ignored_image(img_url):
    return not img_url or any(pattern.lower() in img_url.lower() for pattern in IGNORE_IMAGE_PATTERNS)


def download_local_image(img_url, filename, referer=None):
    if not img_url or is_ignored_image(img_url):
        return ""
    try:
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
        response = requests.get(img_url, headers=headers, timeout=15, stream=True)
        response.raise_for_status()
        if "image" not in response.headers.get("Content-Type", "").lower():
            return ""
        content = response.content
        if len(content) < 2500 or len(content) > 5 * 1024 * 1024:
            return ""
        os.makedirs("data/images", exist_ok=True)
        with open(os.path.join("data", "images", filename), "wb") as image_file:
            image_file.write(content)
        return f"./data/images/{filename}"
    except Exception as exc:
        print(f"[안내] 이미지 다운로드 실패: {img_url[:100]} ({exc})")
        return ""


def scrape_generic_article(url, max_len=4000):
    if not is_valid_article_url(url):
        return "", ""
    try:
        response = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        image_url = ""
        meta = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if meta and meta.get("content"):
            candidate = urljoin(response.url, meta["content"].strip())
            if not is_ignored_image(candidate):
                image_url = candidate
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe", "svg"]):
            tag.decompose()
        body = ""
        if trafilatura is not None:
            body = trafilatura.extract(html, url=response.url, include_comments=False, include_tables=False, favor_recall=True) or ""
        if len(body.strip()) < 180:
            container = (
                soup.find("article")
                or soup.find("div", class_=re.compile(r"(article|entry-content|post-content|article-body|news-content|article_view|contents_style)", re.I))
                or soup.body
            )
            if container:
                if not image_url:
                    for image in container.find_all("img"):
                        candidate = image.get("data-src") or image.get("data-lazy-src") or image.get("data-original") or image.get("src") or ""
                        candidate = urljoin(response.url, candidate.strip())
                        if candidate and not is_ignored_image(candidate):
                            image_url = candidate
                            break
                paragraphs = [item.get_text(" ", strip=True) for item in container.find_all(["p", "li"]) if len(item.get_text(" ", strip=True)) >= 15]
                if paragraphs:
                    body = "\n\n".join(dict.fromkeys(paragraphs))
        return image_url, clean_article_text(body, max_len=max_len)
    except Exception as exc:
        print(f"[경고] 원문 추출 실패 ({url[:80]}): {exc}")
        return "", ""


def scrape_naver_blog_post(url, img_idx=0):
    try:
        match = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url)
        target = f"https://m.blog.naver.com/{match.group(1)}/{match.group(2)}" if match else url.replace("://blog.naver.com", "://m.blog.naver.com")
        response = requests.get(target, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        container = soup.select_one(".se-main-container") or soup.select_one("#postViewArea") or soup.select_one(".post_ct")
        if not container:
            return "", ""
        image_url = ""
        for image in container.find_all("img"):
            candidate = image.get("data-lazy-src") or image.get("data-src") or image.get("src") or ""
            if candidate and "postfiles.pstatic.net" in candidate and not is_ignored_image(candidate):
                image_url = candidate
                break
        local_image = download_local_image(image_url, f"blog_{img_idx}.jpg", referer=target) if image_url else ""
        return local_image, clean_article_text(container.get_text("\n", strip=True), max_len=4000)
    except Exception:
        return "", ""


def normalize_blog_rss(raw_url):
    url = (raw_url or "").strip()
    if not url:
        return ""
    if "rss.blog.naver.com" in url:
        return url
    match = re.search(r"blog\.naver\.com/([a-zA-Z0-9_-]+)", url)
    if match:
        return f"https://rss.blog.naver.com/{match.group(1)}.xml"
    if not url.startswith("http") and re.match(r"^[a-zA-Z0-9_-]+$", url):
        return f"https://rss.blog.naver.com/{url}.xml"
    return url


def fetch_weather(city):
    if not city:
        return ""
    try:
        geo = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(city)}&count=1&language=ko&format=json", timeout=10).json()
        results = geo.get("results") or []
        if not results:
            return f"{city} (날씨 정보 없음)"
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        weather = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FSeoul&forecast_days=1", timeout=10).json()
        daily = weather.get("daily", {})
        code = (daily.get("weathercode") or [0])[0]
        low = round((daily.get("temperature_2m_min") or [0])[0])
        high = round((daily.get("temperature_2m_max") or [0])[0])
        rain = (daily.get("precipitation_probability_max") or [0])[0]
        return f"{city} {WMO_DESC.get(code, '흐림')} {low}°/{high}° (강수 {rain}%)"
    except Exception as exc:
        print(f"[경고] 날씨 조회 실패: {exc}")
        return ""


def build_search_query(raw_query, global_excludes_str):
    query = (raw_query or "").strip()
    if not query:
        return ""
    if " OR " in query or "(" in query:
        base_query = query
    else:
        positives, negatives = [], []
        for token in [part.strip() for part in re.split(r"[,]", query) if part.strip()]:
            parts = token.split()
            pos = [part for part in parts if not part.startswith("-")]
            neg = [part for part in parts if part.startswith("-") and len(part) > 1]
            if pos:
                phrase = " ".join(pos)
                positives.append(f'"{phrase}"' if " " in phrase else phrase)
            negatives.extend(neg)
        if not positives:
            return query
        base_query = " OR ".join(positives)
        if negatives:
            base_query += " " + " ".join(negatives)
    excludes = [word.strip().lstrip("-") for word in re.split(r"[,\s]+", global_excludes_str or "") if word.strip()]
    additions = [f"-{word}" for word in excludes[:10] if f"-{word}" not in base_query]
    return f"{base_query} {' '.join(additions)}" if additions else base_query


def fetch_news_candidates(categories, global_excludes, disliked_urls):
    candidates, seen_titles = [], set()
    for category in categories:
        category_name = category.get("name", "뉴스").strip()
        raw_query = category.get("query", "").strip()
        if not raw_query:
            continue
        query = build_search_query(raw_query, global_excludes)
        rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:2d&hl=ko&gl=KR&ceid=KR:ko"
        time.sleep(1.5)
        feed = fetch_feed_safe(rss_url)
        count = 0
        for entry in feed.entries[:8]:
            title, link = entry.get("title", "").strip(), entry.get("link", "").strip()
            if not title or not link or link in disliked_urls:
                continue
            normalized = re.sub(r"\W+", "", title)[:25]
            if normalized in seen_titles:
                continue
            seen_titles.add(normalized)
            source = entry.get("source", {}).get("title", "") if entry.get("source") else ""
            if not source and " - " in title:
                title, source = title.rsplit(" - ", 1)
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
            candidates.append({"id": len(candidates), "category": category_name, "title": title.strip(), "source": source.strip() or "뉴스", "url": link, "snippet": summary[:400]})
            count += 1
            if count >= 4:
                break
    print(f"뉴스 후보 {len(candidates)}건 수집 완료")
    return candidates


def fetch_research_candidates(research_feeds, history):
    enabled = [feed_config for feed_config in research_feeds if feed_config.get("enabled", True) and feed_config.get("url")]
    if not enabled:
        return []
    published = history.get("research", [])
    start = history.get("research_idx", 0) % len(enabled)
    candidates = []
    for offset in range(len(enabled)):
        current = (start + offset) % len(enabled)
        feed_config = enabled[current]
        feed = fetch_feed_safe(feed_config["url"])
        for entry in feed.entries[:5]:
            link = entry.get("link", "").strip()
            if not link or link in published:
                continue
            image, body = scrape_generic_article(link, max_len=1600)
            if not body:
                body = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)[:1500]
            if len(body) < 180:
                continue
            local_image = download_local_image(image, "research_0.jpg", referer=link) if image else ""
            candidates.append({"source_name": feed_config.get("name", "심리학 연구"), "title": entry.get("title", "").strip(), "url": link, "image": local_image or image, "body": body})
            history["research_idx"] = (current + 1) % len(enabled)
            break
        if candidates:
            break
    print(f"최신 심리 실험 연구 후보 {len(candidates)}건 수집 완료")
    return candidates


def is_recent_entry(entry, hours=48):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return bool(parsed and (time.time() - calendar.timegm(parsed)) <= hours * 3600)


def fetch_blog_articles(blogs_cfg, published_urls):
    picked, image_index = [], 0
    for blog in blogs_cfg:
        if not blog.get("enabled", True) or not blog.get("url"):
            continue
        feed = fetch_feed_safe(normalize_blog_rss(blog["url"]))
        if not feed.entries:
            continue
        mode = blog.get("mode", "unread")
        if mode == "new_only":
            entries = [item for item in feed.entries[:10] if item.get("link", "").split("?")[0] not in published_urls and is_recent_entry(item, 48)][:3]
        else:
            entries = [item for item in feed.entries[:30] if item.get("link", "").split("?")[0] not in published_urls][:4]
        if not entries:
            continue
        selected = None
        for entry in entries:
            link = entry.get("link", "").split("?")[0]
            title = entry.get("title", "").strip()
            if "blog.naver.com" in link:
                image, body = scrape_naver_blog_post(link, image_index)
            else:
                raw_image, body = scrape_generic_article(link, max_len=4000)
                image = download_local_image(raw_image, f"blog_{image_index}.jpg", referer=link) or raw_image
            if len(body) >= 80:
                selected = {"blog_name": blog.get("name", "구독 블로그"), "title": title, "url": link, "image": image, "full_article": body, "published": entry.get("published", "")[:16]}
                image_index += 1
                break
        if not selected:
            entry = entries[0]
            link = entry.get("link", "").split("?")[0]
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text("\n", strip=True)
            selected = {"blog_name": blog.get("name", "구독 블로그"), "title": entry.get("title", "").strip(), "url": link, "image": "", "full_article": clean_article_text(summary, 3500) or "원문 링크에서 전체 내용을 확인하세요.", "published": entry.get("published", "")[:16]}
        picked.append(selected)
        if len(picked) >= 3:
            break
    print(f"구독 블로그 아티클 {len(picked)}편 수집 완료")
    return picked


def build_quote_instruction(quote_mode, custom_prompt):
    if quote_mode == "none":
        return 'edition_quote 값은 반드시 빈 문자열("")로 비워두세요.'
    if quote_mode == "famous_quote":
        return "edition_quote에는 아침에 읽기 좋은 철학·문학·심리학 명언 1줄과 출처(인물명)를 한국어로 작성하세요."
    if quote_mode == "ai_cheer":
        return "edition_quote에는 오늘 하루를 기분 좋게 시작할 수 있는 AI 편집장의 따뜻하고 담백한 응원 1줄을 작성하���요."
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
    article_count = max(3, min(9, int(config.get("article_count", 6))))
    sub_count = article_count - 1
    likes = "\n".join(f"- [{item.get('category', '')}] {item.get('title', '')}" for item in prefs.get("likes", [])[-15:]) or "없음"
    dislikes = "\n".join(f"- [{item.get('category', '')}] {item.get('title', '')}" for item in prefs.get("dislikes", [])[-15:]) or "없음"
    quote_instruction = build_quote_instruction(config.get("quote_mode", "news_comment"), config.get("quote_custom_prompt", ""))
    prompt = f"""당신은 개인 맞춤형 조간 신문의 수석 편집장입니다.
뉴스 후보를 선택해 JSON으로 응답하세요.
[독자 취향: 좋아요]\n{likes}\n[독자 취향: 싫어요]\n{dislikes}
[뉴스 후보]\n{json.dumps(news_candidates, ensure_ascii=False)}
[연구 원문]\n{json.dumps(research_candidates[0] if research_candidates else None, ensure_ascii=False)}
규칙: 총 {article_count}건(톱기사 1건, articles {sub_count}건)을 후보에서 선택하세요. 기사를 창작하지 말고 후보의 정수 id와 category만 반환하세요. {quote_instruction} 연구 원문이 있으면 연구 리포트의 full_article, takeaway, journal_info를 작성하고 없으면 null로 둡니다.
JSON 스키마: {{"edition_quote":"문자열","lead_story":{{"id":0,"category":"카테고리명"}},"articles":[{{"id":1,"category":"카테고리명"}}],"research_report":{{"title":"한국어 제목","journal_info":"출처 요약","full_article":"문단1\\n\\n문단2","takeaway":"핵심 시사점"}}}}"""
    models = []
    try:
        for model in client.models.list():
            name = (model.name or "").replace("models/", "")
            if name.startswith("gemini-") and not any(part in name for part in ("embedding", "imagen", "veo", "tts", "audio", "vision", "robotics", "computer-use")):
                models.append(name)
    except Exception as exc:
        print(f"[경고] 모델 목록 자동 조회 실패: {exc}")
    if not models:
        models = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"]

    def priority(name):
        return (0 if "flash-lite" in name else 1 if "flash" in name else 2 if "pro" in name else 3, name)

    last_error = None
    for model_name in sorted(set(models), key=priority)[:6]:
        for attempt in range(2):
            try:
                print(f"[안내] {model_name} 모델로 편집 요청 중... ({attempt + 1}/2)")
                response = client.models.generate_content(model=model_name, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4))
                return json.loads(response.text)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                if any(code in message for code in ("404", "NOT_FOUND", "400")):
                    print(f"[안내] {model_name} 사용 불가, 다음 모델로 전환합니다.")
                    break
                if any(code in message for code in ("503", "429", "UNAVAILABLE")):
                    wait = 6 * (attempt + 1)
                    print(f"[안내] {model_name} 서버 혼잡. {wait}초 후 재시도...")
                    time.sleep(wait)
                else:
                    raise
    raise last_error


def main():
    now = datetime.now(KST)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    date_string = now.strftime("%Y-%m-%d")
    date_korean = f"{now.year}년 {now.month}월 {now.day}일 ({weekdays[now.weekday()]})"
    config = load_json("config.json", {})
    history = load_history()
    prefs = load_json("data/preferences.json", {"likes": [], "dislikes": [], "saved": []})
    disliked_urls = {item.get("url") for item in prefs.get("dislikes", []) if item.get("url")}
    show_weather = config.get("show_weather", True)
    weather = fetch_weather(config.get("weather_city", "대구")) if show_weather else ""
    news_candidates = fetch_news_candidates(config.get("categories", []), config.get("exclude_keywords", ""), disliked_urls)
    research_candidates = fetch_research_candidates(config.get("research_feeds", []), history)
    blog_articles = fetch_blog_articles(config.get("blogs", []), history.get("blogs", []))
    ai_result = generate_edition_with_gemini(config, news_candidates, research_candidates, prefs)
    candidate_map = {item["id"]: item for item in news_candidates}

    def enrich_article(item, image_name):
        if not item:
            return None
        source = candidate_map.get(item.get("id"), {})
        raw_url = source.get("url", "")
        article_url = decode_google_news_url(raw_url)
        decoded = bool(article_url) and "news.google.com" not in article_url
        print(f"[디버그URL] {image_name} | 변환성공={decoded} | 원래URL={raw_url[:80]} | 반환URL={article_url[:120]}")
        raw_image, body = scrape_generic_article(article_url) if decoded else ("", "")
        local_image = download_local_image(raw_image, image_name, referer=article_url) if raw_image else ""
        final_image = local_image or raw_image
        final_body = body if len(body) >= 180 else (source.get("snippet") or "")
        body_source = "원문추출" if len(body) >= 180 else "RSS요약"
        print(f"[수집] {image_name} | {body_source} | 사진={bool(final_image)} | 길이={len(final_body)} | URL={article_url[:60]}")
        return {"category": item.get("category") or source.get("category", "뉴스"), "title": source.get("title") or item.get("title", ""), "source": source.get("source", "뉴스"), "url": article_url, "image": final_image, "full_article": final_body, "takeaway": ""}

    lead_story = enrich_article(ai_result.get("lead_story"), "lead.jpg")
    articles = []
    for index, article in enumerate(ai_result.get("articles") or []):
        enriched = enrich_article(article, f"art_{index}.jpg")
        if enriched and enriched.get("title"):
            articles.append(enriched)

    research_report = None
    if research_candidates and ai_result.get("research_report"):
        candidate = research_candidates[0]
        report = ai_result["research_report"]
        research_report = {"source_name": candidate["source_name"], "title": report.get("title") or candidate["title"], "journal_info": report.get("journal_info", candidate["source_name"]), "url": candidate["url"], "image": candidate.get("image", ""), "full_article": report.get("full_article", ""), "takeaway": report.get("takeaway", "")}
        history["research"].append(candidate["url"])
        history["research"] = history["research"][-200:]
    for blog in blog_articles:
        if blog.get("url"):
            history["blogs"].append(blog["url"])
    history["blogs"] = history["blogs"][-300:]
    edition = {"paper_name": config.get("paper_name", "DAILY NEWS"), "date": date_string, "date_kr": date_korean, "show_weather": show_weather, "weather": weather, "edition_quote": ai_result.get("edition_quote", ""), "lead_story": lead_story, "articles": articles, "research_report": research_report, "blog_articles": blog_articles}
    save_json("data/today.json", edition)
    save_json("data/published_history.json", history)
    print(f"[완료] {date_korean} 신문 발행 성공! (기사 {1 + len(articles)}건, 연구 {1 if research_report else 0}건, 블로그 {len(blog_articles)}건)")


if __name__ == "__main__":
    main()

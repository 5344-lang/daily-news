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

try:
    import trafilatura
except ImportError:
    trafilatura = None

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
BLOCKED_HOSTS = {
    "news.google.com", "google.com", "www.google.com", "google-analytics.com",
    "www.google-analytics.com", "googletagmanager.com", "googlesyndication.com",
    "doubleclick.net", "gstatic.com", "fonts.gstatic.com", "fonts.googleapis.com",
    "googleusercontent.com", "w3.org", "angular.dev",
}
IGNORED_IMAGE_PARTS = (
    "tistory_admin/static", "opengraph.png", "no_image", "noimage", "default_image",
    "og_default", "google_news", "news.google.com", "googleusercontent.com", "gstatic.com",
)
WMO_DESC = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "짙은 안개",
    51: "가벼운 이슬비", 53: "이슬비", 55: "강한 이슬비", 61: "약한 비", 63: "비",
    65: "강한 비", 71: "약한 눈", 73: "눈", 75: "강한 눈", 80: "소나기",
    81: "소나기", 82: "강한 소나기", 95: "뇌우", 96: "우박 동반 뇌우", 99: "강한 뇌우",
}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
            return value
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_valid_article_url(value):
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https") or not host or "." not in host:
            return False
        return not any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS)
    except (TypeError, ValueError):
        return False


def _get_google_token(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("articles", "read"):
        if marker in parts and parts.index(marker) + 1 < len(parts):
            return parts[parts.index(marker) + 1]
    return ""


def _decode_batchexecute(url):
    """Resolve current Google News article tokens using Google's garturl RPC."""
    token = _get_google_token(url)
    if not token:
        return ""
    page = requests.get(url, headers=HEADERS, timeout=(5, 15), allow_redirects=True)
    if is_valid_article_url(page.url):
        return page.url
    soup = BeautifulSoup(page.text, "html.parser")
    signature_tag = soup.find(attrs={"data-n-a-sg": True, "data-n-a-ts": True})
    if not signature_tag:
        return ""

    payload = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None,
          None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None,
         0, 0, None, 0],
        token,
        int(signature_tag["data-n-a-ts"]),
        signature_tag["data-n-a-sg"],
    ], separators=(",", ":"))
    request_body = json.dumps([[['Fbv4je', payload, None, "generic"]]], separators=(",", ":"))
    response = requests.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data={"f.req": request_body}, timeout=(5, 15),
    )
    response.raise_for_status()
    for line in response.text.splitlines():
        if "garturlres" not in line:
            continue
        try:
            outer = json.loads(line)
            for record in outer:
                if not isinstance(record, list):
                    continue
                for cell in record:
                    if not isinstance(cell, list) or len(cell) < 3 or cell[0] != "Fbv4je":
                        continue
                    inner = json.loads(cell[2]) if isinstance(cell[2], str) else cell[2]
                    if isinstance(inner, list):
                        for item in inner:
                            if isinstance(item, str) and is_valid_article_url(item):
                                return item
        except (ValueError, TypeError, IndexError):
            continue
    return ""


def decode_google_news_url(url):
    if not url:
        return ""
    if "news.google.com" not in (urlparse(url).hostname or "").lower():
        return url if is_valid_article_url(url) else ""

    if gnewsdecoder is not None:
        try:
            result = gnewsdecoder(url, interval=1)
            decoded = result.get("decoded_url", "") if isinstance(result, dict) and result.get("status") else ""
            if is_valid_article_url(decoded):
                return decoded
            if isinstance(result, dict) and result.get("message"):
                print(f"[안내] googlenewsdecoder 응답: {result.get('message')}")
        except Exception as exc:
            print(f"[안내] googlenewsdecoder 실패: {exc}")
    try:
        decoded = _decode_batchexecute(url)
        if is_valid_article_url(decoded):
            return decoded
    except Exception as exc:
        print(f"[안내] Google News batchexecute 디코딩 실패: {exc}")
    return ""


def clean_article_text(text, max_len=4000):
    if not text:
        return ""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).replace("\u200b", "").strip()
        if len(line) < 15 or any(x.lower() in line.lower() for x in (
            "무단전재", "재배포 금지", "저작권자", "copyright", "제보하기", "구독하기",
        )):
            continue
        lines.append(line)
    return "\n\n".join(lines)[:max_len].strip()


def is_ignored_image(url):
    return not url or any(part in url.lower() for part in IGNORED_IMAGE_PARTS)


def download_local_image(img_url, filename, referer=None):
    if not img_url or is_ignored_image(img_url) or not is_valid_article_url(img_url):
        return ""
    try:
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
        response = requests.get(img_url, headers=headers, timeout=(5, 15), stream=True)
        response.raise_for_status()
        if "image" not in response.headers.get("Content-Type", "").lower():
            return ""
        content = response.content
        if len(content) < 2500 or len(content) > 5 * 1024 * 1024:
            return ""
        os.makedirs("data/images", exist_ok=True)
        path = os.path.join("data", "images", filename)
        with open(path, "wb") as image_file:
            image_file.write(content)
        return f"./data/images/{filename}"
    except Exception as exc:
        print(f"[안내] 이미지 다운로드 실패 ({filename}): {exc}")
        return ""


def scrape_generic_article(url, max_len=4000):
    if not is_valid_article_url(url):
        return "", ""
    try:
        response = requests.get(url, headers=HEADERS, timeout=(5, 18), allow_redirects=True)
        response.raise_for_status()
        final_url = response.url
        if not is_valid_article_url(final_url):
            return "", ""
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        image_url = ""
        for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
            meta = soup.find("meta", attrs=attrs)
            candidate = urljoin(final_url, (meta.get("content") or "").strip()) if meta else ""
            if is_valid_article_url(candidate) and not is_ignored_image(candidate):
                image_url = candidate
                break

        extracted = trafilatura.extract(html, include_comments=False, include_tables=False) if trafilatura else ""
        if extracted and len(extracted.strip()) > 120:
            body = clean_article_text(extracted, max_len)
        else:
            article = soup.find("article") or soup.find(
                ["main"], class_=re.compile(r"article|content|story|news", re.I)
            ) or soup.find("div", class_=re.compile(
                r"article|entry-content|post-content|contents_style|article-body|news-content", re.I
            ))
            container = article or soup.body
            if container:
                for tag in container(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
                    tag.decompose()
                if not image_url:
                    for img in container.find_all("img"):
                        candidate = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
                        candidate = urljoin(final_url, candidate.strip())
                        if is_valid_article_url(candidate) and not is_ignored_image(candidate):
                            image_url = candidate
                            break
                paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
                text = "\n".join(p for p in paragraphs if len(p) > 25) or container.get_text("\n", strip=True)
                body = clean_article_text(text, max_len)
            else:
                body = ""
        return image_url, body
    except Exception as exc:
        print(f"[안내] 원문 스크래핑 실패 ({url[:90]}): {exc}")
        return "", ""


def fetch_feed_safe(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=(5, 15))
        response.raise_for_status()
        return feedparser.parse(response.content)
    except Exception as exc:
        print(f"[경고] RSS 불러오기 실패 ({url[:100]}): {exc}")
        return feedparser.FeedParserDict(entries=[])


def build_search_query(raw_query, excludes):
    query = (raw_query or "").strip()
    if not query:
        return ""
    if " OR " not in query and "(" not in query:
        tokens = [x.strip() for x in query.split(",") if x.strip()]
        if len(tokens) > 1:
            query = " OR ".join(f'"{x}"' if " " in x else x for x in tokens)
    excluded = [x.strip().lstrip("-") for x in re.split(r"[,\s]+", excludes or "") if x.strip()][:10]
    return query + (" " + " ".join("-" + x for x in excluded if "-" + x not in query) if excluded else "")


def fetch_news_candidates(categories, excludes, disliked_urls):
    candidates, seen = [], set()
    for category in categories:
        query = build_search_query(category.get("query", ""), excludes)
        if not query:
            continue
        rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:2d&hl=ko&gl=KR&ceid=KR:ko"
        feed = fetch_feed_safe(rss_url)
        count = 0
        for entry in feed.entries[:10]:
            title, link = entry.get("title", "").strip(), entry.get("link", "").strip()
            if not title or not link or link in disliked_urls:
                continue
            normalized = re.sub(r"\W+", "", title)[:25]
            if normalized in seen:
                continue
            seen.add(normalized)
            source = entry.get("source", {}).get("title", "")
            if not source and " - " in title:
                title, source = title.rsplit(" - ", 1)
            summary_soup = BeautifulSoup(entry.get("summary", ""), "html.parser")
            snippet = summary_soup.get_text(" ", strip=True)
            candidates.append({"id": len(candidates), "category": category.get("name", "뉴스"),
                               "title": title.strip(), "source": source.strip() or "뉴스",
                               "url": link, "snippet": snippet[:400]})
            count += 1
            if count >= 4:
                break
    print(f"뉴스 후보 {len(candidates)}건 수집 완료")
    return candidates


def fetch_research_candidates(feeds, history):
    enabled = [x for x in feeds if x.get("enabled", True) and x.get("url")]
    if not enabled:
        return []
    start = history.get("research_idx", 0) % len(enabled)
    for offset in range(len(enabled)):
        idx = (start + offset) % len(enabled)
        feed = fetch_feed_safe(enabled[idx]["url"])
        for entry in feed.entries[:5]:
            link = entry.get("link", "").strip()
            if not link or link in history.get("research", []):
                continue
            image, body = scrape_generic_article(link, max_len=1600)
            if len(body) < 180:
                body = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)[:1500]
            if len(body) >= 180:
                local = download_local_image(image, "research_0.jpg", link) if image else ""
                history["research_idx"] = (idx + 1) % len(enabled)
                return [{"source_name": enabled[idx].get("name", "심리학 연구"),
                         "title": entry.get("title", "").strip(), "url": link,
                         "image": local or image, "body": body}]
    return []


def normalize_blog_rss(raw_url):
    value = (raw_url or "").strip()
    if "rss.blog.naver.com" in value:
        return value
    match = re.search(r"blog\.naver\.com/([a-zA-Z0-9_-]+)", value)
    if match:
        return f"https://rss.blog.naver.com/{match.group(1)}.xml"
    return value


def fetch_blog_articles(configured, published):
    results = []
    for blog in configured:
        if not blog.get("enabled", True) or not blog.get("url"):
            continue
        feed = fetch_feed_safe(normalize_blog_rss(blog["url"]))
        for entry in feed.entries[:10]:
            link = entry.get("link", "").split("?")[0]
            if not link or link in published:
                continue
            if "blog.naver.com" in link:
                match = re.search(r"blog\.naver\.com/([^/]+)/([0-9]+)", link)
                target = f"https://m.blog.naver.com/{match.group(1)}/{match.group(2)}" if match else link
                img, body = scrape_generic_article(target, max_len=4000)
            else:
                img, body = scrape_generic_article(link, max_len=4000)
            if len(body) < 80:
                body = clean_article_text(BeautifulSoup(entry.get("summary", ""), "html.parser").get_text("\n", strip=True), 3500)
            local = download_local_image(img, f"blog_{len(results)}.jpg", link) if img else ""
            results.append({"blog_name": blog.get("name", "구독 블로그"),
                            "title": entry.get("title", "").strip(), "url": link,
                            "image": local or img, "full_article": body or "원문 링크에서 전체 내용을 확인하세요.",
                            "published": entry.get("published", "")[:16]})
            if len(results) >= 3:
                return results
            break
    return results


def fetch_weather(city):
    if not city:
        return ""
    try:
        geo = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(city)}&count=1&language=ko&format=json", timeout=10).json()
        places = geo.get("results") or []
        if not places:
            return f"{city} (날씨 정보 없음)"
        lat, lon = places[0]["latitude"], places[0]["longitude"]
        data = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FSeoul&forecast_days=1", timeout=10).json().get("daily", {})
        code = (data.get("weathercode") or [0])[0]
        low, high = round((data.get("temperature_2m_min") or [0])[0]), round((data.get("temperature_2m_max") or [0])[0])
        rain = (data.get("precipitation_probability_max") or [0])[0]
        return f"{city} {WMO_DESC.get(code, '흐림')} {low}°/{high}° (강수 {rain}%)"
    except Exception as exc:
        print(f"[경고] 날씨 조회 실패: {exc}")
        return ""


def generate_edition(config, candidates, research, prefs):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
    client = genai.Client(api_key=api_key)
    count = max(3, min(9, int(config.get("article_count", 6))))
    prompt = f'''당신은 개인 맞춤형 조간 신문의 편집장입니다. 아래 뉴스 후보 중 기사 {count}개를 고르고 JSON으로 반환하세요.
후보의 id/category만 반환하고 제목이나 본문을 새로 만들지 마세요. lead_story는 한 개, articles는 나머지 {count - 1}개입니다.
뉴스 후보: {json.dumps(candidates, ensure_ascii=False)}
연구 원문: {json.dumps(research[0] if research else None, ensure_ascii=False)}
좋아요: {json.dumps(prefs.get("likes", [])[-15:], ensure_ascii=False)}
싫어요: {json.dumps(prefs.get("dislikes", [])[-15:], ensure_ascii=False)}
JSON 스키마: {{"edition_quote":"문자열","lead_story":{{"id":0,"category":"카테고리"}},"articles":[{{"id":1,"category":"카테고리"}}],"research_report":null}}'''
    models = []
    try:
        for model in client.models.list():
            name = (model.name or "").replace("models/", "")
            if name.startswith("gemini-") and "embedding" not in name and "imagen" not in name:
                models.append(name)
    except Exception as exc:
        print(f"[안내] 모델 목록 조회 실패: {exc}")
    if not models:
        models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    models.sort(key=lambda name: ("flash-lite" not in name, "flash" not in name, name))
    last_error = None
    for name in models[:6]:
        try:
            response = client.models.generate_content(model=name, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4))
            return json.loads(response.text)
        except Exception as exc:
            last_error = exc
            print(f"[안내] 모델 {name} 실패: {exc}")
    raise last_error


def main():
    now = datetime.now(KST)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    config = load_json("config.json", {})
    history = load_json("data/published_history.json", {})
    history.setdefault("research", [])
    history.setdefault("blogs", [])
    prefs = load_json("data/preferences.json", {"likes": [], "dislikes": [], "saved": []})
    disliked = {item.get("url") for item in prefs.get("dislikes", []) if item.get("url")}
    show_weather = config.get("show_weather", True)
    weather = fetch_weather(config.get("weather_city", "대구")) if show_weather else ""
    news = fetch_news_candidates(config.get("categories", []), config.get("exclude_keywords", ""), disliked)
    research = fetch_research_candidates(config.get("research_feeds", []), history)
    blogs = fetch_blog_articles(config.get("blogs", []), history["blogs"])
    ai = generate_edition(config, news, research, prefs)
    by_id = {entry["id"]: entry for entry in news}

    def enrich(item, filename):
        if not item:
            return None
        source = by_id.get(item.get("id"), {})
        original = source.get("url", "")
        url = decode_google_news_url(original)
        print(f"[디버그URL] {filename} | 변환성공={bool(url)} | 원래URL={original[:80]} | 반환URL={url[:120] if url else '디코딩 실패'}")
        image, body = scrape_generic_article(url, max_len=4000) if url else ("", "")
        local_image = download_local_image(image, filename, referer=url) if image else ""
        final_image = local_image or image
        final_body = body or source.get("snippet", "")
        print(f"[수집] {filename} | {'원문추출' if body else 'RSS요약'} | 사진={bool(final_image)} | 길이={len(final_body)} | URL={url[:80]}")
        return {"category": item.get("category") or source.get("category", "뉴스"),
                "title": source.get("title") or item.get("title", ""),
                "source": source.get("source", "뉴스"), "url": url or original,
                "image": final_image, "full_article": final_body, "takeaway": ""}

    lead = enrich(ai.get("lead_story"), "lead.jpg")
    articles = [enrich(item, f"art_{idx}.jpg") for idx, item in enumerate(ai.get("articles") or [])]
    articles = [item for item in articles if item and item.get("title")]
    report = None
    if research and ai.get("research_report"):
        rc, rr = research[0], ai["research_report"]
        report = {"source_name": rc["source_name"], "title": rr.get("title") or rc["title"],
                  "journal_info": rr.get("journal_info", rc["source_name"]), "url": rc["url"],
                  "image": rc.get("image", ""), "full_article": rr.get("full_article", ""),
                  "takeaway": rr.get("takeaway", "")}
        history["research"].append(rc["url"])
        history["research"] = history["research"][-200:]
    history["blogs"] = (history["blogs"] + [item["url"] for item in blogs if item.get("url")])[-300:]
    edition = {"paper_name": config.get("paper_name", "DAILY NEWS"), "date": now.strftime("%Y-%m-%d"),
               "date_kr": f"{now.year}년 {now.month}월 {now.day}일 ({weekdays[now.weekday()]})",
               "show_weather": show_weather, "weather": weather, "edition_quote": ai.get("edition_quote", ""),
               "lead_story": lead, "articles": articles, "research_report": report, "blog_articles": blogs}
    save_json("data/today.json", edition)
    save_json("data/published_history.json", history)
    print(f"[완료] {edition['date_kr']} 신문 발행 성공! (기사 {bool(lead) + len(articles)}건, 연구 {bool(report)}건, 블로그 {len(blogs)}건)")


if __name__ == "__main__":
    main()

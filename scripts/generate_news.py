import os
import re
import glob
import hashlib
import json
import time
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
BLOCKED_HOSTS = {
    "news.google.com", "google.com", "google-analytics.com", "googletagmanager.com",
    "googlesyndication.com", "doubleclick.net", "gstatic.com", "fonts.gstatic.com",
    "fonts.googleapis.com", "googleusercontent.com", "w3.org", "angular.dev",
}
IGNORED_IMAGE_PARTS = (
    "tistory_admin/static", "opengraph.png", "no_image", "noimage", "default_image",
    "og_default", "google_news", "news.google.com", "googleusercontent.com", "gstatic.com",
)
MIN_ARTICLE_BODY = 250
MAX_TOPIC_SEARCHES = 2
STOPWORDS = {
    "관련", "기자", "단독", "속보", "오늘", "지난", "이번", "대한", "통해", "위해",
    "에서", "으로", "까지", "하는", "했다", "한다", "됐다", "된다", "수능", "뉴스",
    "기준", "정리", "전망", "이유", "무엇", "어떻게", "다시", "및", "등", "한편",
}
WMO_DESC = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "짙은 안개",
    51: "가벼운 이슬비", 53: "이슬비", 55: "강한 이슬비", 61: "약한 비", 63: "비",
    65: "강한 비", 71: "약한 눈", 73: "눈", 75: "강한 눈", 80: "소나기",
    81: "소나기", 82: "강한 소나기", 95: "뇌우", 96: "우박 동반 뇌우", 99: "강한 뇌우",
}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
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
    parts = [part for part in urlparse(url).path.split("/") if part]
    for marker in ("articles", "read"):
        if marker in parts and parts.index(marker) + 1 < len(parts):
            return parts[parts.index(marker) + 1]
    return ""


def _find_garturl_result(node):
    if isinstance(node, dict):
        for value in node.values():
            found = _find_garturl_result(value)
            if found:
                return found
    elif isinstance(node, list):
        if len(node) >= 3 and node[0] == "wrb.fr" and node[1] == "Fbv4je":
            payload = node[2]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    return ""
            if isinstance(payload, list):
                if len(payload) > 1 and payload[0] == "garturlres" and is_valid_article_url(payload[1]):
                    return payload[1]
                for value in payload:
                    found = _find_garturl_result(value)
                    if found:
                        return found
        for value in node:
            found = _find_garturl_result(value)
            if found:
                return found
    return ""


def _decode_batchexecute(url):
    token = _get_google_token(url)
    if not token:
        return ""
    session = requests.Session()
    page = session.get(url, headers=HEADERS, timeout=(5, 15), allow_redirects=True)
    page.raise_for_status()
    if is_valid_article_url(page.url):
        return page.url
    soup = BeautifulSoup(page.text, "html.parser")
    signature = soup.find(attrs={"data-n-a-sg": True, "data-n-a-ts": True})
    if not signature:
        return ""
    inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None,
          None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        token, int(signature["data-n-a-ts"]), signature["data-n-a-sg"],
    ], separators=(",", ":"))
    body = json.dumps([[['Fbv4je', inner, None, "generic"]]], separators=(",", ":"))
    response = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        params={"rpcids": "Fbv4je", "source-path": urlparse(page.url).path, "hl": "ko", "gl": "KR", "rt": "c"},
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "Origin": "https://news.google.com", "Referer": page.url},
        data={"f.req": body}, timeout=(5, 15),
    )
    response.raise_for_status()
    text = response.text.lstrip()
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[-1]
    for line in text.splitlines():
        try:
            line = line.strip()
            if not line:
                continue
            start = line.find("[[")
            parsed = json.loads(line[start:]) if start >= 0 else json.loads(line)
            found = _find_garturl_result(parsed)
            if found:
                return found
        except (ValueError, TypeError):
            continue
    return ""


def decode_google_news_url(url):
    if not url:
        return ""
    if "news.google.com" not in (urlparse(url).hostname or "").lower():
        return url if is_valid_article_url(url) else ""
    try:
        decoded = _decode_batchexecute(url)
        if is_valid_article_url(decoded):
            return decoded
    except Exception as exc:
        print(f"[안내] Google News URL 디코딩 실패: {exc}")
    if gnewsdecoder:
        try:
            result = gnewsdecoder(url, interval=1)
            decoded = result.get("decoded_url", "") if isinstance(result, dict) and result.get("status") else ""
            if is_valid_article_url(decoded):
                return decoded
        except Exception as exc:
            print(f"[안내] googlenewsdecoder 실패: {exc}")
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


def make_takeaway(text, title=""):
    cleaned = clean_article_text(text or "", 4000)
    if cleaned:
        sentence = re.split(r"(?<=[.!?。？！])\s+|\n+", cleaned, maxsplit=1)[0].strip()[:180].rstrip(" ,;:")
        if sentence:
            return sentence if sentence[-1] in ".!?。？！" else sentence + "다."
    clean_title = re.sub(r"\s+-\s+[^-]+$", "", (title or "")).strip()
    return f"{clean_title}의 핵심 내용을 확인할 수 있습니다." if clean_title else "기사의 핵심 내용을 원문에서 확인할 수 있습니다."


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
        # 슬롯 이름(lead.jpg 등)은 매일 덮어써져 스크랩 기사의 사진이 바뀐다 → 기사 URL 기반 고유 이름 사용
        filename = hashlib.sha1((referer or img_url).encode("utf-8")).hexdigest()[:12] + ".jpg"
        os.makedirs("data/images", exist_ok=True)
        path = os.path.join("data", "images", filename)
        with open(path, "wb") as image_file:
            image_file.write(content)
        return f"./data/images/{filename}"
    except Exception as exc:
        print(f"[안내] 이미지 다운로드 실패 ({filename}): {exc}")
        return ""


def cleanup_images(edition):
    """오늘 발행본·스크랩 보관함이 쓰지 않는 이미지만 삭제(스크랩한 기사 사진은 메일 발송 때까지 유지)."""
    used = set()

    def collect(obj):
        if isinstance(obj, dict):
            img = obj.get("image")
            if isinstance(img, str) and img.startswith("./data/images/"):
                used.add(os.path.basename(img))
            for v in obj.values():
                collect(v)
        elif isinstance(obj, list):
            for v in obj:
                collect(v)

    collect(edition)
    try:
        with open("data/preferences.json", "r", encoding="utf-8") as f:
            collect(json.load(f).get("saved", []))
    except Exception:
        pass
    for path in glob.glob("data/images/*"):
        if os.path.basename(path) not in used:
            try:
                os.remove(path)
            except OSError:
                pass


def _amp_candidates(url):
    parsed = urlparse(url)
    host, path = (parsed.hostname or "").lower(), parsed.path.rstrip("/")
    if host in {"news1.kr", "www.news1.kr"} and re.fullmatch(r"/society/[^/]+/\d+", path):
        return [urljoin(url, path + "/amp")]
    return []


def scrape_generic_article(url, max_len=4000):
    """Try canonical URL then a known public AMP variant; does not evade access controls."""
    if not is_valid_article_url(url):
        return "", ""
    for target in [url] + _amp_candidates(url):
        try:
            parsed = urlparse(target)
            headers = {**HEADERS, "Referer": f"{parsed.scheme}://{parsed.netloc}/"}
            response = requests.get(target, headers=headers, timeout=(5, 18), allow_redirects=True)
            if response.status_code == 403:
                print(f"[안내] 게시자 접근 거부(403), 공개 AMP만 확인: {target}")
                continue
            response.raise_for_status()
            final_url = response.url
            if not is_valid_article_url(final_url):
                continue
            response.encoding = response.apparent_encoding or "utf-8"
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            image = ""
            for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
                meta = soup.find("meta", attrs=attrs)
                candidate = urljoin(final_url, (meta.get("content") or "").strip()) if meta else ""
                if is_valid_article_url(candidate) and not is_ignored_image(candidate):
                    image = candidate
                    break
            extracted = trafilatura.extract(html, include_comments=False, include_tables=False) if trafilatura else ""
            body = clean_article_text(extracted, max_len) if extracted else ""
            if len(body) < MIN_ARTICLE_BODY:
                container = soup.find("article") or soup.find(["main"], class_=re.compile(r"article|content|story|news", re.I)) or soup.find(
                    "div", class_=re.compile(r"article|entry-content|post-content|contents_style|article-body|news-content|news1_article", re.I)) or soup.body
                if container:
                    for tag in container(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
                        tag.decompose()
                    if not image:
                        for img in container.find_all("img"):
                            candidate = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
                            candidate = urljoin(final_url, candidate.strip())
                            if is_valid_article_url(candidate) and not is_ignored_image(candidate):
                                image = candidate
                                break
                    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
                    text = "\n".join(p for p in paragraphs if len(p) > 25) or container.get_text("\n", strip=True)
                    body = clean_article_text(text, max_len)
            if len(body) >= MIN_ARTICLE_BODY:
                return image, body
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"[안내] 원문 요청 실패 HTTP {status} ({target[:90]})")
        except Exception as exc:
            print(f"[안내] 원문 스크래핑 실패 ({target[:90]}): {exc}")
    return "", ""


def fetch_feed_safe(url, retries=2):
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=(5, 15))
            if response.status_code in (429, 503) and attempt < retries:
                delay = min(2 ** (attempt + 1), 8)
                print(f"[안내] RSS {response.status_code}, {delay}초 후 재시도")
                time.sleep(delay)
                continue
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            return parsed if parsed.entries else feedparser.FeedParserDict(entries=[])
        except requests.RequestException as exc:
            if attempt < retries and getattr(getattr(exc, "response", None), "status_code", None) in (429, 503):
                time.sleep(min(2 ** (attempt + 1), 8))
                continue
            print(f"[경고] RSS 불러오기 실패 ({url[:100]}): {exc}")
            return feedparser.FeedParserDict(entries=[])
    return feedparser.FeedParserDict(entries=[])


def build_search_query(raw_query, excludes):
    raw = (raw_query or "").strip()
    if not raw:
        return ""
    positives, negatives = [], []
    for token in raw.split(","):
        words = token.strip().split()
        positive = [word for word in words if not (word.startswith("-") and len(word) > 1)]
        negatives.extend(word[1:] for word in words if word.startswith("-") and len(word) > 1)
        if positive:
            phrase = " ".join(positive)
            positives.append(f'"{phrase}"' if " " in phrase else phrase)
    query = " OR ".join(positives) if positives else raw
    for word in [x.strip().lstrip("-") for x in re.split(r"[,\s]+", excludes or "") if x.strip()][:10]:
        if word not in negatives:
            negatives.append(word)
    return query + (" " + " ".join("-" + word for word in negatives[:15]) if negatives else "")


def fetch_news_candidates(categories, excludes, disliked_urls):
    candidates, seen = [], set()
    for category in categories:
        query = build_search_query(category.get("query", ""), excludes)
        if not query:
            continue
        feed = fetch_feed_safe(f"https://news.google.com/rss/search?q={quote_plus(query)}+when:2d&hl=ko&gl=KR&ceid=KR:ko")
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
            snippet = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
            candidates.append({"id": len(candidates), "category": category.get("name", "뉴스"),
                               "title": title.strip(), "source": source.strip() or "뉴스", "url": link,
                               "snippet": snippet[:400]})
            count += 1
            if count >= 4:
                break
        time.sleep(1.2)
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
            image, body = scrape_generic_article(link, max_len=4000)
            if len(body) < MIN_ARTICLE_BODY:
                body = clean_article_text(BeautifulSoup(entry.get("summary", ""), "html.parser").get_text("\n", strip=True), 3500)
            if len(body) >= MIN_ARTICLE_BODY:
                local = download_local_image(image, "research_0.jpg", link) if image else ""
                history["research_idx"] = (idx + 1) % len(enabled)
                return [{"source_name": enabled[idx].get("name", "심리학 연구"), "title": entry.get("title", "").strip(),
                         "url": link, "image": local or image, "body": body}]
    return []


def normalize_blog_rss(raw_url):
    value = (raw_url or "").strip()
    if "rss.blog.naver.com" in value:
        return value
    match = re.search(r"blog\.naver\.com/([a-zA-Z0-9_-]+)", value)
    return f"https://rss.blog.naver.com/{match.group(1)}.xml" if match else value


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
            target = link
            if "blog.naver.com" in link:
                match = re.search(r"blog\.naver\.com/([^/]+)/([0-9]+)", link)
                if match:
                    target = f"https://m.blog.naver.com/{match.group(1)}/{match.group(2)}"
            image, body = scrape_generic_article(target, max_len=4000)
            if len(body) < 80:
                body = clean_article_text(BeautifulSoup(entry.get("summary", ""), "html.parser").get_text("\n", strip=True), 3500)
            local = download_local_image(image, f"blog_{len(results)}.jpg", link) if image else ""
            results.append({"blog_name": blog.get("name", "구독 블로그"), "title": entry.get("title", "").strip(),
                            "url": link, "image": local or image, "full_article": body or "원문 링크에서 전체 내용을 확인하세요.",
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
        daily = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FSeoul&forecast_days=1", timeout=10).json().get("daily", {})
        code = (daily.get("weathercode") or [0])[0]
        low, high = round((daily.get("temperature_2m_min") or [0])[0]), round((daily.get("temperature_2m_max") or [0])[0])
        rain = (daily.get("precipitation_probability_max") or [0])[0]
        return f"{city} {WMO_DESC.get(code, '흐림')} {low}°/{high}° (강수 {rain}%)"
    except Exception as exc:
        print(f"[경고] 날씨 조회 실패: {exc}")
        return ""


def _article_keywords(title):
    text = re.sub(r"\s+-\s+[^-]+$", " ", title or "")
    text = re.sub(r"\[[^]]*\]|【[^】]*】|…|\.\.\.|[‘’“”\"'·,:;!?()[\]{}<>]", " ", text)
    tokens = re.findall(r"[가-힣]{2,}|[A-Za-z0-9]{2,}", text.lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) >= 2}


def _keyword_similarity(left, right):
    a, b = _article_keywords(left), _article_keywords(right)
    return len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0


def _source_host(candidate):
    host = (urlparse(candidate.get("url", "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _search_related_candidates(candidate, visited_urls):
    keywords = sorted(_article_keywords(candidate.get("title", "")), key=lambda x: (-len(x), x))[:6]
    if len(keywords) < 2:
        return []
    feed = fetch_feed_safe(f"https://news.google.com/rss/search?q={quote_plus(' '.join(keywords))}&hl=ko&gl=KR&ceid=KR:ko", retries=1)
    result = []
    original_host = _source_host(candidate)
    for entry in feed.entries[:8]:
        url, title = entry.get("link", "").strip(), entry.get("title", "").strip()
        if not url or url in visited_urls or not title:
            continue
        alternative = {"url": url, "title": title, "source": entry.get("source", {}).get("title", "")}
        if _source_host(alternative) == original_host or _keyword_similarity(candidate.get("title", ""), title) < 0.34:
            continue
        alternative.update({"category": candidate.get("category", "뉴스"),
                            "snippet": BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)[:400]})
        result.append(alternative)
    return result


def generate_edition(config, candidates, research, prefs):
    if not candidates:
        raise RuntimeError("뉴스 후보를 하나도 수집하지 못했습니다. 발행을 중단합니다.")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
    client = genai.Client(api_key=api_key)
    count = max(3, min(9, int(config.get("article_count", 6))))
    research_input = research[0] if research else None
    prompt = f'''개인 맞춤 조간신문 편집장입니다. 뉴스 후보 중 기사 {count}개를 고르고 JSON만 반환하세요.
후보 id/category만 반환하고 제목이나 본문을 새로 만들지 마세요. lead_story 한 개, articles 나머지 {count - 1}개입니다.
각 뉴스 후보에는 takeaway를 넣으세요. 제목과 RSS 요약에 근거한 한 문장 핵심 요약입니다.
뉴스 후보: {json.dumps(candidates, ensure_ascii=False)}
연구 원문: {json.dumps(research_input, ensure_ascii=False)}
연구가 있으면 실제 원문 body를 바탕으로 research_report.full_article을 한국어 3~4문단으로 편집하고, takeaway는 핵심 한 문장으로 작성하세요. 추측이나 새 사실은 덧붙이지 말고, 연구가 없으면 null입니다.
좋아요: {json.dumps(prefs.get("likes", [])[-15:], ensure_ascii=False)}
싫어요: {json.dumps(prefs.get("dislikes", [])[-15:], ensure_ascii=False)}
스키마: {{"edition_quote":"문자열","lead_story":{{"id":0,"category":"카테고리","takeaway":"핵심 한 문장"}},"articles":[{{"id":1,"category":"카테고리","takeaway":"핵심 한 문장"}}],"research_report":{{"title":"원문 제목","journal_info":"출처","full_article":"한국어 본문 3~4문단","takeaway":"핵심 한 문장"}} 또는 null}}'''
    models = []
    try:
        for model in client.models.list():
            name = (model.name or "").replace("models/", "")
            if name.startswith("gemini-") and "embedding" not in name and "imagen" not in name:
                if name != "gemini-2.5-flash-lite":
                    models.append(name)
    except Exception as exc:
        print(f"[안내] 모델 목록 조회 실패: {exc}")
    if not models:
        models = ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"]
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
    if not news:
        raise RuntimeError("뉴스 후보가 없습니다. 피드가 복구될 때까지 today.json 발행을 중단합니다.")
    research = fetch_research_candidates(config.get("research_feeds", []), history)
    blogs = fetch_blog_articles(config.get("blogs", []), history["blogs"])
    ai = generate_edition(config, news, research, prefs)
    by_id = {entry["id"]: entry for entry in news}

    def enrich(item, filename, candidate=None):
        if not item:
            return None
        source = candidate or by_id.get(item.get("id"), {})
        original = source.get("url", "")
        url = decode_google_news_url(original)
        print(f"[디버그URL] {filename} | 변환성공={bool(url)} | 원래URL={original[:80]} | 반환URL={url[:120] if url else '디코딩 실패'}")
        image, body = scrape_generic_article(url, max_len=4000) if url else ("", "")
        if len(body) < MIN_ARTICLE_BODY:
            print(f"[안내] {filename}: 원문 길이 {len(body)}자, 대체 기사 탐색")
            return None
        local_image = download_local_image(image, filename, referer=url) if image else ""
        takeaway = clean_article_text(item.get("takeaway", ""), 240) or make_takeaway(body, source.get("title", ""))
        print(f"[수집] {filename} | 원문추출 | 사진={bool(local_image or image)} | takeaway={bool(takeaway)} | 길이={len(body)}")
        return {"category": item.get("category") or source.get("category", "뉴스"),
                "title": source.get("title") or item.get("title", ""), "source": source.get("source", "뉴스"),
                "url": url, "image": local_image or image, "full_article": body, "takeaway": takeaway,
                "_candidate_id": source.get("id")}

    selections = []
    if ai.get("lead_story"):
        selections.append(("lead", ai["lead_story"]))
    selections.extend(("article", item) for item in (ai.get("articles") or []))
    visited_ids, visited_urls = set(), set()
    successful, failed = [], []
    for role, selection in selections:
        source = by_id.get(selection.get("id"), {})
        if source.get("id") is not None:
            visited_ids.add(source["id"])
        if source.get("url"):
            visited_urls.add(source["url"])
        index = sum(1 for r, _ in successful if r == "article")
        article = enrich(selection, "lead.jpg" if role == "lead" else f"art_{index}.jpg", source)
        if article:
            successful.append((role, article))
        else:
            failed.append((role, selection.get("category") or source.get("category", "뉴스"), source))

    alternates = [c for c in news if c.get("id") not in visited_ids and c.get("url") not in visited_urls]
    topic_searches = 0
    for role, category, failed_source in failed:
        ranked = sorted(alternates,
            key=lambda c: (-_keyword_similarity(failed_source.get("title", ""), c.get("title", "")),
                           c.get("category") != category, c.get("id", 0)))
        replacement = None
        tested = set()
        for candidate in ranked:
            if _source_host(candidate) == _source_host(failed_source):
                continue
            if _keyword_similarity(failed_source.get("title", ""), candidate.get("title", "")) < 0.34:
                continue
            cid, url = candidate.get("id"), candidate.get("url", "")
            if cid in visited_ids or url in visited_urls:
                continue
            visited_ids.add(cid)
            visited_urls.add(url)
            tested.add(cid)
            index = sum(1 for r, _ in successful if r == "article")
            replacement = enrich({"id": cid, "category": candidate.get("category")},
                                 "lead.jpg" if role == "lead" and not any(r == "lead" for r, _ in successful) else f"art_{index}.jpg", candidate)
            if replacement:
                break
        alternates = [c for c in alternates if c.get("id") not in tested and c.get("id") not in visited_ids]

        if replacement is None and topic_searches < MAX_TOPIC_SEARCHES:
            topic_searches += 1
            time.sleep(2)
            for candidate in _search_related_candidates(failed_source, visited_urls):
                url = candidate.get("url", "")
                if not url or url in visited_urls:
                    continue
                visited_urls.add(url)
                index = sum(1 for r, _ in successful if r == "article")
                replacement = enrich({"id": -topic_searches, "category": category, "takeaway": ""},
                                     "lead.jpg" if role == "lead" and not any(r == "lead" for r, _ in successful) else f"art_{index}.jpg", candidate)
                if replacement:
                    break
        if replacement:
            successful.append((role, replacement))
        else:
            print(f"[안내] {category} 기사 대체 후보가 없어 제외합니다. RSS 스니펫은 발행하지 않습니다.")

    successful = [(role, {k: v for k, v in article.items() if not k.startswith("_")}) for role, article in successful]
    lead = next((article for role, article in successful if role == "lead"), None)
    if lead is None and successful:
        lead = successful[0][1]
    articles = [article for role, article in successful if article is not lead]

    report = None
    if research:
        rc = research[0]
        rr = ai.get("research_report") if isinstance(ai.get("research_report"), dict) else {}
        report_body = clean_article_text(rr.get("full_article", ""), 4000)
        if len(report_body) < MIN_ARTICLE_BODY:
            report_body = clean_article_text(rc.get("body", ""), 4000)
        if report_body:
            report_takeaway = clean_article_text(rr.get("takeaway", ""), 240) or make_takeaway(report_body, rr.get("title") or rc.get("title", ""))
            report = {"source_name": rc.get("source_name", "심리학 연구"),
                      "title": rr.get("title") or rc.get("title", "연구 리포트"),
                      "journal_info": rr.get("journal_info") or rc.get("source_name", "심리학 연구"),
                      "url": rc.get("url", ""), "image": rc.get("image", ""),
                      "full_article": report_body, "takeaway": report_takeaway}
            history["research"].append(rc["url"])
            history["research"] = history["research"][-200:]
            print(f"[수집] 연구 리포트 | 본문길이={len(report_body)} | takeaway={bool(report_takeaway)}")
        else:
            print(f"[안내] 연구 원문과 RSS 요약이 없어 리포트 발행 생략: {rc.get('url', '')}")

    history["blogs"] = (history["blogs"] + [item["url"] for item in blogs if item.get("url")])[-300:]
    edition = {"paper_name": config.get("paper_name", "DAILY NEWS"), "date": now.strftime("%Y-%m-%d"),
               "date_kr": f"{now.year}년 {now.month}월 {now.day}일 ({weekdays[now.weekday()]})",
               "show_weather": show_weather, "weather": weather, "edition_quote": ai.get("edition_quote", ""),
               "lead_story": lead, "articles": articles, "research_report": report, "blog_articles": blogs}
    save_json("data/today.json", edition)
    cleanup_images(edition)
    save_json("data/published_history.json", history)
    print(f"[완료] {edition['date_kr']} 신문 발행 성공! (기사 {bool(lead) + len(articles)}건, 연구 {bool(report)}건, 블로그 {len(blogs)}건)")


if __name__ == "__main__":
    main()

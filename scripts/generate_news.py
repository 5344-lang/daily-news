import os
import json
import base64
import random
import smtplib
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
import requests
from bs4 import BeautifulSoup
import trafilatura
from googlenewsdecoder import new_decoderv1

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
TODAY_KR = datetime.now(KST).strftime("%Y년 %m월 %d일")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
TO_EMAIL = os.environ.get("TO_EMAIL", GMAIL_USER).strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}


def get_candidate_models():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    stable_text, preview_text = [], []
    stable_image, preview_image = [], []
    try:
        res = requests.get(url, timeout=30)
        res.raise_for_status()
        for m in res.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            lower = name.lower()
            is_preview = any(x in lower for x in ["preview", "exp", "omni"])
            if "image" in lower:
                (preview_image if is_preview else stable_image).append(name)
            elif "flash" in lower and not any(x in lower for x in ["tts", "audio", "embedding", "lite"]):
                (preview_text if is_preview else stable_text).append(name)
        stable_text.sort(reverse=True)
        preview_text.sort(reverse=True)
        stable_image.sort(reverse=True)
        preview_image.sort(reverse=True)
    except Exception as e:
        print(f"모델 목록 조회 중 경고: {e}")

    text_models = stable_text + preview_text
    image_models = stable_image + preview_image

    for fb in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
        if fb not in text_models:
            text_models.append(fb)
    for fb in ["gemini-2.5-flash-image", "gemini-2.0-flash-exp-image-generation"]:
        if fb not in image_models:
            image_models.append(fb)

    return text_models, image_models


def load_psych_history():
    path = "data/psych_history.json"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_psych_history(history, new_topic):
    if new_topic and new_topic not in history:
        history.append(new_topic)
    os.makedirs("data", exist_ok=True)
    with open("data/psych_history.json", "w", encoding="utf-8") as f:
        json.dump(history[-60:], f, ensure_ascii=False, indent=2)


def resolve_real_url(google_url):
    try:
        decoded = new_decoderv1(google_url, interval=1)
        if decoded.get("status") and decoded.get("decoded_url"):
            return decoded["decoded_url"]
    except Exception:
        pass
    try:
        r = requests.get(google_url, headers=HEADERS, timeout=10, allow_redirects=True)
        return r.url
    except Exception:
        return google_url


def scrape_article(google_url):
    real_url = resolve_real_url(google_url)
    image_url = ""
    full_text = ""
    try:
        res = requests.get(real_url, headers=HEADERS, timeout=12)
        res.encoding = res.apparent_encoding or "utf-8"
        html = res.text

        soup = BeautifulSoup(html, "html.parser")
        og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if og_img and og_img.get("content"):
            cand = og_img["content"].strip()
            if cand.startswith("http"):
                image_url = cand

        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
        if extracted and len(extracted.strip()) > 120:
            full_text = extracted.strip()
        else:
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 30]
            full_text = "\n\n".join(paras[:15])
    except Exception as e:
        print(f"본문 추출 건너뜀 ({real_url[:50]}): {e}")

    return real_url, image_url, full_text[:2800]


def fetch_rss_news(categories):
    collected = []
    for cat in categories:
        q = urllib.parse.quote(cat["query"])
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            raw_link = entry.get("link", "")
            real_url, img, body = scrape_article(raw_link)
            collected.append({
                "category": cat["name"],
                "title": entry.get("title", ""),
                "url": real_url,
                "source": entry.get("source", {}).get("title", "뉴스"),
                "image": img,
                "raw_body": body,
            })
    print(f"총 {len(collected)}건 기사 수집 및 본문/사진 추출 완료")
    return collected


def call_gemini_text(news_items, past_topics, text_models):
    prompt = f"""
당신은 고전 신문 'DAILY NEWS'의 편집장 겸 심리학 칼럼니스트입니다.
아래 오늘({TODAY_KR}) 수집된 뉴스(원문 텍스트와 이미지 URL 포함)를 바탕으로, 독자가 외부 링크로 나가지 않고 이 신문 안에서 기사를 완독할 수 있도록 편집해 JSON으로만 응답하세요.

[요구사항]
1. lead_story: 오늘 가장 흥미롭고 중요한 1면 톱기사 1건
   - full_article: 수집된 raw_body의 핵심 사실, 수치, 인용구를 빠짐없이 살려 4~6문단으로 충실하게 정리한 전체 기사 본문 (문단 구분은 \\n\\n 사용). raw_body가 부족하면 제목과 맥락을 바탕으로 충실히 보완할 것.
   - takeaway: 핵심 포인트 한 줄
   - image: 입력 데이터의 image URL을 그대로 유지
2. articles: 카테고리별로 골고루 5건 선별
   - full_article: 독자가 원문을 따로 찾아보지 않아도 되도록 핵심 내용·배경·전망을 3~5문단으로 충실하게 담은 전체 기사 본문 (문단 구분은 \\n\\n 사용)
   - image: 입력 데이터의 image URL을 그대로 유지
3. psych_column: 매일 1개의 흥미로운 심리학·인지과학·상담이론 개념 소개
   - 이미 다룬 주제({json.dumps(past_topics, ensure_ascii=False)})는 제외할 것.
   - concept_name: 심리학 용어명
   - title: 흥미로운 칼럼 제목
   - caption: 만화 하단에 들어갈 묵직한 한 줄 요약 및 학자/연도 (예: "능력이 낮을수록 자신의 오류를 알아차릴 인지 도구도 없다. (Flavell, 1979)")
   - summary: 개념 설명 3~4문장
   - real_case: 일상 예시 또는 유명 심리학 실험 사례 2~3문장
   - image_prompt: 아래 스타일 가이드에 맞춘 **영어** 이미지 생성 프롬프트.
     * 스타일: Clean modern Korean educational comic webtoon illustration, bold black ink outlines, expressive cartoon characters, flat cel-shading, contrasting warm and cool split background colors, wide 16:9 comic panel.
     * 구도: 주제에 맞춰 '좌우 2단 비교 컷(Split-screen comparison)', '시소/저울 같은 직관적 시각 비유', 또는 '실제 실험 장면 스토리텔링' 중 하나를 구체적으로 묘사할 것.
     * 중요: 이미지 안에 글자/텍스트/말풍선 글씨는 절대 넣지 말 것 ("no text, no words, no letters, no speech bubbles"). 표정과 시각적 상징물만으로 표현할 것.

[출력 JSON 스키마]
{{
  "edition_quote": "오늘 신문 제호 아래 들어갈 짧은 명언 또는 편집장 한마디",
  "lead_story": {{
    "category": "카테고리명",
    "title": "신문 헤드라인 스타일 제목",
    "full_article": "문단1\\n\\n문단2\\n\\n문단3\\n\\n문단4",
    "takeaway": "핵심 포인트 한 줄",
    "source": "언론사명",
    "url": "원문 URL",
    "image": "입력된 이미지 URL 그대로"
  }},
  "articles": [
    {{
      "category": "카테고리명",
      "title": "제목",
      "full_article": "문단1\\n\\n문단2\\n\\n문단3",
      "source": "언론사명",
      "url": "원문 URL",
      "image": "입력된 이미지 URL 그대로"
    }}
  ],
  "psych_column": {{
    "concept_name": "심리학 용어명",
    "title": "칼럼 제목",
    "caption": "만화 하단 한 줄 통찰 (학자, 연도)",
    "summary": "개념 설명",
    "real_case": "실험 또는 일상 사례",
    "image_prompt": "English comic illustration prompt..."
  }}
}}

[수집된 뉴스]
{json.dumps(news_items, ensure_ascii=False)}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }

    for model_name in text_models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        res = requests.post(endpoint, json=payload, timeout=90)
        if res.status_code != 200:
            print(f"[{model_name}] 건너뜀 ({res.status_code})")
            continue
        print(f"사용된 텍스트 모델: {model_name}")
        text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

    raise RuntimeError("사용 가능한 Gemini 텍스트 모델을 찾지 못했습니다.")


def generate_psych_illustration(image_prompt, image_models):
    if not image_prompt:
        return ""
    os.makedirs("data", exist_ok=True)
    img_path = "data/today_psych.png"

    # 1순위: Gemini 이미지 모델 시도
    payload = {
        "contents": [{"parts": [{"text": image_prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    for model_name in image_models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(endpoint, json=payload, timeout=90)
            if res.status_code != 200:
                continue
            parts = res.json()["candidates"][0]["content"]["parts"]
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    with open(img_path, "wb") as f:
                        f.write(base64.b64decode(inline["data"]))
                    print(f"Gemini 삽화 생성 완료! ({model_name})")
                    return "./data/today_psych.png"
        except Exception:
            pass

    # 2순위: 무료 무키 이미지 생성 폴백 (Pollinations)
    try:
        encoded = urllib.parse.quote(image_prompt)
        seed = random.randint(1, 999999)
        free_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=576&nologo=true&seed={seed}"
        res = requests.get(free_url, headers=HEADERS, timeout=60)
        if res.status_code == 200 and len(res.content) > 5000:
            with open(img_path, "wb") as f:
                f.write(res.content)
            print("무료 이미지 생성기(Pollinations)로 심리학 학습만화 삽화 저장 완료!")
            return "./data/today_psych.png"
    except Exception as e:
        print(f"무료 이미지 폴백 오류: {e}")

    return ""


def send_gmail(edition, site_url):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and TO_EMAIL):
        print("Gmail 설정이 없어 메일 발송을 건너뜁니다.")
        return

    lead = edition["lead_story"]
    psych = edition.get("psych_column", {})
    lead_preview = lead.get("full_article", "").split("\n\n")[0]
    items_html = "".join(
        f"<li style='margin-bottom:8px;'><b>[{a['category']}]</b> {a['title']}</li>"
        for a in edition["articles"][:5]
    )
    html = f"""
    <div style="font-family: Georgia, serif; max-width: 560px; margin: 0 auto; padding: 20px; border: 2px solid #111; background: #faf7f0; color: #111;">
      <div style="text-align: center; border-bottom: 3px double #111; padding-bottom: 10px; margin-bottom: 16px;">
        <h1 style="margin: 0; letter-spacing: 3px;">DAILY NEWS</h1>
        <p style="margin: 4px 0 0; font-size: 13px;">{TODAY_KR} 조간 발행</p>
      </div>
      <p style="font-size: 12px; font-weight: bold; margin-bottom: 4px;">[1면 톱기사 · {lead['category']}]</p>
      <h2 style="margin-top: 0;">{lead['title']}</h2>
      <p style="line-height: 1.6;">{lead_preview}</p>
      <hr style="border: 0; border-top: 1px solid #111; margin: 18px 0;">
      <p style="font-weight: bold;">오늘의 주요 기사</p>
      <ul style="padding-left: 18px; line-height: 1.6;">{items_html}</ul>
      <div style="background: #efe9dc; border: 1px solid #111; padding: 12px; margin-top: 16px;">
        <p style="margin: 0 0 4px; font-size: 12px; font-weight: bold;">🧠 오늘의 심리학 학습만화</p>
        <p style="margin: 0; font-weight: bold;">{psych.get('title', '')}</p>
        <p style="margin: 4px 0 0; font-size: 13px;">{psych.get('caption', '')}</p>
      </div>
      <div style="text-align: center; margin-top: 24px;">
        <a href="{site_url}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; font-weight: bold;">
          📰 오늘의 신문 전체 기사 & 만화 읽기
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

    text_models, image_models = get_candidate_models()
    past_topics = load_psych_history()
    news = fetch_rss_news(config["categories"])
    ai_data = call_gemini_text(news, past_topics, text_models)

    psych = ai_data.get("psych_column", {})
    psych["image"] = generate_psych_illustration(psych.get("image_prompt", ""), image_models)
    save_psych_history(past_topics, psych.get("concept_name", ""))

    edition = {
        "date": TODAY,
        "date_kr": TODAY_KR,
        "paper_name": config.get("paper_name", "DAILY NEWS"),
        "edition_quote": ai_data.get("edition_quote", ""),
        "lead_story": ai_data["lead_story"],
        "articles": ai_data["articles"],
        "psych_column": psych,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/today.json", "w", encoding="utf-8") as f:
        json.dump(edition, f, ensure_ascii=False, indent=2)

    send_gmail(edition, config.get("site_url", ""))
    print(f"{TODAY} DAILY NEWS 생성 완료!")


if __name__ == "__main__":
    main()

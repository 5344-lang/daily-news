import os
import json
import base64
import smtplib
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
import requests

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
TODAY_KR = datetime.now(KST).strftime("%Y년 %m월 %d일")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
TO_EMAIL = os.environ.get("TO_EMAIL", GMAIL_USER).strip()


def get_candidate_models():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    text_models = []
    image_models = []
    try:
        res = requests.get(url, timeout=30)
        res.raise_for_status()
        for m in res.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            lower = name.lower()
            if "image" in lower:
                image_models.append(name)
            elif "flash" in lower and not any(x in lower for x in ["tts", "audio", "embedding", "lite"]):
                text_models.append(name)
        text_models.sort(reverse=True)
        image_models.sort(reverse=True)
    except Exception as e:
        print(f"모델 목록 조회 중 경고: {e}")

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


def fetch_rss_news(categories):
    collected = []
    for cat in categories:
        q = urllib.parse.quote(cat["query"])
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:
            img = ""
            if "media_content" in entry and entry.media_content:
                img = entry.media_content[0].get("url", "")
            collected.append({
                "category": cat["name"],
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": entry.get("source", {}).get("title", "뉴스"),
                "image": img,
            })
    return collected


def call_gemini_text(news_items, past_topics, text_models):
    prompt = f"""
당신은 고전 신문 'DAILY NEWS'의 편집장 겸 심리학 칼럼니스트입니다.
아래 오늘({TODAY_KR}) 수집된 뉴스를 엄선하고, 마지막 면에 실을 '오늘의 심리학 학습만화 칼럼'을 작성해 JSON으로만 응답하세요.

[요구사항]
1. lead_story: 오늘 가장 흥미롭고 중요한 1면 톱기사 1건 (요약 4~5문장, 핵심 한 줄 포함)
2. articles: 카테고리별로 골고루 6~8건 선별 (각 요약 2~3문장, 품격 있는 신문 문어체)
3. psych_column: 매일 1개의 흥미로운 심리학·인지과학·상담이론 개념 소개
   - 이미 다룬 주제({json.dumps(past_topics, ensure_ascii=False)})는 제외할 것.
   - concept_name: 심리학 용어명 (예: 더닝-크루거 효과, 확증 편향, 자이가르닉 효과 등)
   - title: 흥미로운 칼럼 제목
   - caption: 만화 하단에 들어갈 묵직한 한 줄 요약 및 학자/연도 (예: "능력이 낮을수록 자신의 오류를 알아차릴 인지 도구도 없다. (Flavell, 1979)")
   - summary: 개념 설명 3~4문장 (쉽고 명쾌하게)
   - real_case: 일상 예시 또는 유명 심리학 실험 사례 2~3문장
   - image_prompt: 아래 스타일 가이드에 맞춘 **영어** 이미지 생성 프롬프트.
     * 스타일: Clean modern Korean educational comic / webtoon illustration style, crisp bold black ink outlines, expressive cartoon characters, flat cel-shading with contrasting warm and cool split background colors, 16:9 wide horizontal comic panel.
     * 구도: 주제에 맞춰 '좌우 2단 비교 컷(Split-screen comparison)', '시소/저울 같은 직관적 시각 비유', 또는 '실제 실험 장면 스토리텔링' 중 하나를 선택해 구체적으로 묘사할 것.
     * 중요: 이미지 안에 글자/텍스트/말풍선 글씨는 절대 넣지 말 것 ("No text, no speech bubbles, no letters, no words inside the image"). 오직 표정, 행동, 아이콘(별, 물음표, 돋보기, 전구 등)만으로 개념이 직관적으로 이해되게 묘사할 것.

[출력 JSON 스키마]
{{
  "edition_quote": "오늘 신문 제호 아래 들어갈 짧은 명언 또는 편집장 한마디",
  "lead_story": {{
    "category": "카테고리명",
    "title": "신문 헤드라인 스타일 제목",
    "summary": "4~5문장 요약",
    "takeaway": "핵심 포인트 한 줄",
    "source": "언론사명",
    "url": "원문 URL",
    "image": "이미지 URL (없으면 빈 문자열)"
  }},
  "articles": [
    {{
      "category": "카테고리명",
      "title": "제목",
      "summary": "2~3문장 요약",
      "source": "언론사명",
      "url": "원문 URL",
      "image": "이미지 URL (없으면 빈 문자열)"
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
        "generationConfig": {"responseMimeType": "application/json"},
    }

    for model_name in text_models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        res = requests.post(endpoint, json=payload, timeout=60)
        if res.status_code == 404:
            print(f"[{model_name}] 사용 불가(404), 다음 모델 시도...")
            continue
        res.raise_for_status()
        print(f"사용된 텍스트 모델: {model_name}")
        text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

    raise RuntimeError("사용 가능한 Gemini 텍스트 모델을 찾지 못했습니다. API 키를 확인해 주세요.")


def generate_psych_illustration(image_prompt, image_models):
    if not image_prompt:
        return ""
    payload = {
        "contents": [{"parts": [{"text": image_prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    for model_name in image_models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(endpoint, json=payload, timeout=90)
            if res.status_code != 200:
                print(f"[{model_name}] 이미지 생성 건너뜀 ({res.status_code})")
                continue
            parts = res.json()["candidates"][0]["content"]["parts"]
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    os.makedirs("data", exist_ok=True)
                    img_path = "data/today_psych.png"
                    with open(img_path, "wb") as f:
                        f.write(base64.b64decode(inline["data"]))
                    print(f"심리학 학습만화 삽화 생성 완료! (모델: {model_name})")
                    return "./data/today_psych.png"
        except Exception as e:
            print(f"[{model_name}] 삽화 생성 오류: {e}")
    print("사용 가능한 이미지 모델이 없어 텍스트 칼럼만 발행합니다.")
    return ""


def send_gmail(edition, site_url):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and TO_EMAIL):
        print("Gmail 설정이 없어 메일 발송을 건너뜁니다.")
        return

    lead = edition["lead_story"]
    psych = edition.get("psych_column", {})
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
      <p style="line-height: 1.6;">{lead['summary']}</p>
      <hr style="border: 0; border-top: 1px solid #111; margin: 18px 0;">
      <p style="font-weight: bold;">오늘의 주요 단신</p>
      <ul style="padding-left: 18px; line-height: 1.6;">{items_html}</ul>
      <div style="background: #efe9dc; border: 1px solid #111; padding: 12px; margin-top: 16px;">
        <p style="margin: 0 0 4px; font-size: 12px; font-weight: bold;">🧠 오늘의 심리학 학습만화</p>
        <p style="margin: 0; font-weight: bold;">{psych.get('title', '')}</p>
        <p style="margin: 4px 0 0; font-size: 13px;">{psych.get('caption', '')}</p>
      </div>
      <div style="text-align: center; margin-top: 24px;">
        <a href="{site_url}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; font-weight: bold;">
          📰 오늘의 신문 & 심리학 만화 보기
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

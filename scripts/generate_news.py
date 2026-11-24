import os
import json
import re
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


def call_gemini(news_items):
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
당신은 고전 신문 'DAILY NEWS'의 편집장입니다.
아래 오늘({TODAY_KR}) 수집된 뉴스 목록을 보고 중복을 제거한 뒤, 독자가 아침에 읽기 좋게 엄선하여 JSON으로만 응답하세요.

[요구사항]
1. lead_story: 오늘 가장 흥미롭고 중요한 1면 톱기사 1건 (요약 4~5문장, 핵심 한 줄 포함)
2. articles: 카테고리별로 골고루 6~8건 선별 (각 요약 2~3문장, 품격 있는 신문 문어체)
3. crossword_words: 오늘 기사 내용 및 상식과 관련된 2~4글자 한글 단어 12개와 힌트(열쇠).
   - 반드시 한글만 사용(띄어쓰기/숫자/영어 금지).
   - 십자말풀이로 교차하기 좋게 서로 겹치는 글자(예: 심, 리, 기, 술, 인, 공, 경, 제, 사, 회, 문, 화 등)가 포함된 단어들로 구성할 것.

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
  "crossword_words": [
    {{ "word": "한글단어", "clue": "힌트 설명" }}
  ]
}}

[수집된 뉴스]
{json.dumps(news_items, ensure_ascii=False)}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    res = requests.post(endpoint, json=payload, timeout=60)
    res.raise_for_status()
    text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def build_crossword(candidates, size=9):
    clean = []
    seen = set()
    for item in candidates:
        w = re.sub(r"[^가-힣]", "", item.get("word", ""))
        if 2 <= len(w) <= 5 and w not in seen:
            seen.add(w)
            clean.append({"word": w, "clue": item.get("clue", "")})

    grid = [["" for _ in range(size)] for _ in range(size)]
    placed = []

    if not clean:
        return {"size": 0, "grid": [], "across": [], "down": [], "tiles": []}

    # 첫 단어 중앙 가로 배치
    first = clean.pop(0)
    r0 = size // 2
    c0 = (size - len(first["word"])) // 2
    for i, ch in enumerate(first["word"]):
        grid[r0][c0 + i] = ch
    placed.append({"word": first["word"], "clue": first["clue"], "row": r0, "col": c0, "dir": "across"})

    def can_place(word, row, col, direction):
        dr, dc = (0, 1) if direction == "across" else (1, 0)
        if row < 0 or col < 0 or row + dr * (len(word) - 1) >= size or col + dc * (len(word) - 1) >= size:
            return False
        # 앞뒤 칸 비어있는지 확인
        br, bc = row - dr, col - dc
        ar, ac = row + dr * len(word), col + dc * len(word)
        if 0 <= br < size and 0 <= bc < size and grid[br][bc] != "":
            return False
        if 0 <= ar < size and 0 <= ac < size and grid[ar][ac] != "":
            return False

        intersections = 0
        for i, ch in enumerate(word):
            r, c = row + dr * i, col + dc * i
            cur = grid[r][c]
            if cur != "":
                if cur != ch:
                    return False
                intersections += 1
            else:
                # 교차하지 않는 칸의 양옆이 이미 차있으면 인접 충돌 방지
                if direction == "across":
                    if (r > 0 and grid[r - 1][c] != "") or (r < size - 1 and grid[r + 1][c] != ""):
                        return False
                else:
                    if (c > 0 and grid[r][c - 1] != "") or (c < size - 1 and grid[r][c + 1] != ""):
                        return False
        return intersections >= 1

    for _ in range(2):
        for item in list(clean):
            if len(placed) >= 6:
                break
            w = item["word"]
            found = False
            for p in placed:
                if found:
                    break
                new_dir = "down" if p["dir"] == "across" else "across"
                for pi, pch in enumerate(p["word"]):
                    if found:
                        break
                    for wi, wch in enumerate(w):
                        if pch == wch:
                            pr = p["row"] + (pi if p["dir"] == "down" else 0)
                            pc = p["col"] + (pi if p["dir"] == "across" else 0)
                            sr = pr - (wi if new_dir == "down" else 0)
                            sc = pc - (wi if new_dir == "across" else 0)
                            if can_place(w, sr, sc, new_dir):
                                dr, dc = (0, 1) if new_dir == "across" else (1, 0)
                                for i, ch in enumerate(w):
                                    grid[sr + dr * i][sc + dc * i] = ch
                                placed.append({"word": w, "clue": item["clue"], "row": sr, "col": sc, "dir": new_dir})
                                clean.remove(item)
                                found = True
                                break

    # 여백 자르기
    rows = [r for r in range(size) for c in range(size) if grid[r][c]]
    cols = [c for r in range(size) for c in range(size) if grid[r][c]]
    min_r, max_r = min(rows), max(rows)
    min_c, max_c = min(cols), max(cols)

    cropped = [row[min_c:max_c + 1] for row in grid[min_r:max_r + 1]]
    num_map = {}
    num = 1
    across, down = [], []

    for p in sorted(placed, key=lambda x: (x["row"], x["col"])):
        r, c = p["row"] - min_r, p["col"] - min_c
        if (r, c) not in num_map:
            num_map[(r, c)] = num
            num += 1
        entry = {"num": num_map[(r, c)], "row": r, "col": c, "length": len(p["word"]), "clue": p["clue"], "answer": p["word"]}
        if p["dir"] == "across":
            across.append(entry)
        else:
            down.append(entry)

    tiles = sorted(list({ch for row in cropped for ch in row if ch}))
    return {
        "rows": len(cropped),
        "cols": len(cropped[0]),
        "grid": cropped,
        "numbers": {f"{r},{c}": n for (r, c), n in num_map.items()},
        "across": across,
        "down": down,
        "tiles": tiles,
    }


def send_gmail(edition, site_url):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and TO_EMAIL):
        print("Gmail 설정이 없어 메일 발송을 건너뜁니다.")
        return

    lead = edition["lead_story"]
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
      <div style="text-align: center; margin-top: 24px;">
        <a href="{site_url}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; font-weight: bold;">
          📰 오늘의 신문 & 십자말풀이 열기
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

    news = fetch_rss_news(config["categories"])
    ai_data = call_gemini(news)
    crossword = build_crossword(ai_data.get("crossword_words", []))

    edition = {
        "date": TODAY,
        "date_kr": TODAY_KR,
        "paper_name": config.get("paper_name", "DAILY NEWS"),
        "edition_quote": ai_data.get("edition_quote", ""),
        "lead_story": ai_data["lead_story"],
        "articles": ai_data["articles"],
        "crossword": crossword,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/today.json", "w", encoding="utf-8") as f:
        json.dump(edition, f, ensure_ascii=False, indent=2)

    send_gmail(edition, config.get("site_url", ""))
    print(f"{TODAY} DAILY NEWS 생성 완료!")


if __name__ == "__main__":
    main()
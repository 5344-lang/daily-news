import os
import json
import time
import html
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

KST = timezone(timedelta(hours=9))
PREFS_PATH = "data/preferences.json"
CONFIG_PATH = "config.json"


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


def resolve_image_url(img_path, site_url):
    if not img_path:
        return ""
    img_path = img_path.strip()
    if img_path.startswith("http://") or img_path.startswith("https://"):
        return img_path
    if site_url:
        base = site_url.rstrip("/") + "/"
        return base + img_path.lstrip("./")
    return ""


def build_card_html(item, site_url):
    category = html.escape(item.get("category") or "스크랩")
    source = html.escape(item.get("source") or "")
    title = html.escape(item.get("title") or "제목 없음")
    url = html.escape(item.get("url") or "#")

    raw_body = (item.get("full_article") or "").replace("\n", " ").strip()
    preview_30 = raw_body[:30] + ("..." if len(raw_body) > 30 else "")
    preview_html = html.escape(preview_30) if preview_30 else "원문 링크에서 전체 내용을 확인하세요."

    img_url = resolve_image_url(item.get("image", ""), site_url)
    img_block = ""
    if img_url:
        img_block = (
            f'<img src="{html.escape(img_url)}" alt="기사 이미지" '
            'style="width:100%; max-height:220px; object-fit:cover; display:block; border-bottom:1px solid #e0dcd3;" />'
        )

    meta_line = f"{category}" + (f" · {source}" if source else "")

    return f"""
    <div style="background:#ffffff; border:1px solid #d8d3c8; border-radius:8px; overflow:hidden; margin-bottom:18px;">
      {img_block}
      <div style="padding:16px 18px;">
        <div style="font-size:11px; font-weight:700; color:#666666; letter-spacing:0.4px; margin-bottom:6px;">
          {meta_line}
        </div>
        <div style="font-size:17px; font-weight:800; color:#161616; line-height:1.4; margin-bottom:8px;">
          {title}
        </div>
        <div style="font-size:13.5px; color:#444444; line-height:1.55; margin-bottom:14px;">
          {preview_html}
        </div>
        <a href="{url}" target="_blank" style="display:inline-block; background:#161616; color:#faf7f0; text-decoration:none; font-size:12.5px; font-weight:700; padding:7px 13px; border-radius:4px;">
          원문 기사 보기 &rarr;
        </a>
      </div>
    </div>
    """


def main():
    prefs = load_json(PREFS_PATH, {"likes": [], "dislikes": [], "saved": []})
    saved = prefs.get("saved") or []

    if not saved:
        print("[안내] 스크랩된 기사가 없어 메일 발송을 건너뜁니다.")
        return

    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip() or gmail_user

    if not gmail_user or not gmail_pass:
        print("[경고] GMAIL_USER 또는 GMAIL_APP_PASSWORD 시크릿이 설정되지 않아 메일을 보낼 수 없습니다.")
        return

    config = load_json(CONFIG_PATH, {})
    paper_name = config.get("paper_name", "DAILY NEWS")
    site_url = config.get("site_url", "")
    now_kst = datetime.now(KST)
    date_label = now_kst.strftime("%Y.%m.%d")

    cards_html = "\n".join(build_card_html(item, site_url) for item in saved)
    full_html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8" /></head>
<body style="margin:0; padding:20px 12px; background:#f4efe6; font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;">
  <div style="max-width:560px; margin:0 auto;">
    <div style="text-align:center; padding:18px 16px 14px; border-bottom:3px double #161616; margin-bottom:20px;">
      <div style="font-size:22px; font-weight:900; letter-spacing:2px; color:#161616;">{html.escape(paper_name)}</div>
      <div style="font-size:12.5px; color:#555555; margin-top:6px;">스크랩북 · {date_label} · 총 {len(saved)}건</div>
    </div>
    {cards_html}
    <div style="text-align:center; font-size:11.5px; color:#777777; padding-top:10px;">
      이 메일은 {html.escape(paper_name)}에서 스크랩한 기사들을 모아 자동 발송되었습니다.
    </div>
  </div>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{paper_name} 스크랩] {date_label} (총 {len(saved)}건)"
    msg["From"] = f"{paper_name} <{gmail_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [recipient], msg.as_string())

    print(f"[완료] 스크랩 기사 {len(saved)}건을 {recipient}로 발송했습니다.")

    # 발송 완료 후 보관함 초기화 및 리셋 타임스탬프 기록
    prefs["saved"] = []
    prefs["saved_reset_at"] = int(time.time())
    save_json(PREFS_PATH, prefs)
    print("[완료] 스크랩 보관함을 초기화했습니다.")


if __name__ == "__main__":
    main()

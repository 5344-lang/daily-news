# 내 DAILY NEWS 만들기 (약 15분, 무료)

관심 키워드 뉴스를 매일 아침 AI가 골라 신문으로 만들어 주고, 저장한 기사는 카드뉴스 메일로 보내 주는 개인 신문입니다.
내 키·메일·키워드는 **내 저장소에만** 저장됩니다.

## 준비물
- GitHub 계정 (무료)
- Gemini API 키 — https://aistudio.google.com/apikey (무료 한도로 충분)
- Gmail 앱 비밀번호 — 구글 계정에서 2단계 인증을 켠 뒤 https://myaccount.google.com/apppasswords 에서 발급

## 순서
1. 이 저장소 상단의 **Use this template → Create a new repository**. 저장소는 **Public**으로 만듭니다(무료 Pages 사용).
2. 내 저장소 **Settings → Pages** → Branch를 `main` / `(root)`로 저장.
3. **Settings → Secrets and variables → Actions → New repository secret**에 아래를 등록합니다.
   | 이름 | 값 |
   |---|---|
   | `GEMINI_API_KEY` | Gemini API 키 |
   | `GMAIL_USER` | 보내는 Gmail 주소 |
   | `GMAIL_APP_PASSWORD` | Gmail 앱 비밀번호(16자리) |
   | `RECIPIENT_EMAIL` | 받을 메일 주소 (비우면 보내는 주소로 발송) |
4. **Actions 탭 → `1. Initialize My Paper` → Run workflow** (처음 한 번만. 샘플 데이터를 지우고 내 신문으로 초기화합니다).
5. **Actions 탭 → `Daily News Generator` → Run workflow**. 1~3분 뒤 첫 신문이 발행됩니다.
6. `https://내아이디.github.io/저장소이름/` 접속 → 홈 화면에 추가하면 앱처럼 쓸 수 있습니다.

## 키워드·발행 시간·구독 바꾸기
신문 화면의 **설정**에서 바꿉니다. 처음 한 번 GitHub 토큰이 필요합니다.
- GitHub → Settings → Developer settings → Fine-grained tokens → 내 저장소 **하나만** 선택
- 권한: Contents, Actions, Workflows 모두 **Read and write**
- 토큰은 그 기기 브라우저에만 저장됩니다. 공용 PC에서는 입력하지 마세요.

## 스크랩 메일
- 기사 아래 **저장** → 다음 날 아침 발행 직전에 카드뉴스 메일로 자동 발송되고 보관함이 비워집니다.
- 보관함의 **지금 메일로 보내기**로 바로 보낼 수도 있습니다.

## 알아둘 점
- 저장소가 공개라서 좋아요·저장 기록(기사 제목)도 공개됩니다. 민감한 기사는 저장하지 마세요.
- API 키·비밀번호는 Secrets에만 넣고, 파일에 직접 쓰지 마세요.
- GitHub 예약 실행은 정시보다 몇 분~수십 분 늦을 수 있습니다.

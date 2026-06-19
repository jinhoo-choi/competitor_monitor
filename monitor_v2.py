"""
네이버 카페 부정여론 모니터링 (다중 카페)
- 24시간 내 게시글 중 키워드(한국투자증권/한투/뱅키스/BanKIS) 탐지
- Claude AI로 부정 뉘앙스 분석 및 요약
- 부정 강도 1 이상 게시글 담당자 이메일 발송
- 탐지 없음 / 오류 시 발신자 전용 상태 이메일 발송
"""

import os
import json
import html
import smtplib
import traceback
import re
import random
import time
import urllib.parse
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────
# 환경변수 (GitHub Secrets)
# ─────────────────────────────────────────

GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_APP_PW   = os.environ["GMAIL_APP_PW"]
NOTIFY_EMAIL   = os.environ["NOTIFY_EMAIL"]
RECIPIENTS     = [r.strip() for r in NOTIFY_EMAIL.split(",") if r.strip()]
CC_EMAIL       = os.getenv("EMAIL_CC", "")
CC_RECIPIENTS  = [r.strip() for r in CC_EMAIL.split(",") if r.strip()]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

COOKIE_FILE = "naver_cookies.json"

# ─────────────────────────────────────────
# 카페 설정 (다중 카페)
# ─────────────────────────────────────────

CAFES = [
    {"id": "likeusstock", "num_id": "28497937", "name": "미국주식이 미래다"},
    {"id": "vilab",       "num_id": "11525920", "name": "가치투자연구소"},
    {"id": "yamizal",     "num_id": "30676048", "name": "미국 주식에 미치다"},
    {"id": "geobuk2",     "num_id": "26251287", "name": "거북이 투자법"},
    {"id": "ustock",      "num_id": "15112066", "name": "평생주식카페"},
    {"id": "onepieceholicplus", "num_id": "22290117", "name": "월급쟁이 재테크 연구카페"},
    {"id": "pointns",          "num_id": "25873056", "name": "주식투자는 달팽이처럼"},
    {"id": "wjdrkrjqn",        "num_id": "30786704", "name": "정가를 거부하는 사람들"},
    {"id": "stocktraining",    "num_id": "29798500", "name": "나는 주식트레이더다"},
    {"id": "dlxogns01",        "num_id": "23676262", "name": "배당 투자자 모임"},
    {"id": "engmstudy",        "num_id": "14028420", "name": "짠돌이카페"},
    {"id": "hayate1",          "num_id": "11560463", "name": "주식광장"},
    {"id": "divclub",          "num_id": "31050378", "name": "은퇴후 50년"},
]

KEYWORDS    = ["한국투자증권", "한투", "뱅키스", "BanKIS"]
DB_FILE      = "seen_posts.json"
TIME_WINDOW = 24   # 탐지 범위 (시간) - 24시간 기준, 일 1회 발송

# ─────────────────────────────────────────
# 부정 강도 판단 기준 (score 0~10)
# ─────────────────────────────────────────
# 0   : 무관 (단순 언급·중립)
# 1~3 : 경미한 불만
# 4~6 : 명확한 불만·비판·피해 호소
# 7~10: 강한 비판·확산 가능성 높음
# ※ SCORE_THRESHOLD 이상인 경우만 담당자 알림 발송
SCORE_THRESHOLD = 1   # 부정 강도 이 값 이상인 경우만 알림 발송
MAX_ALERTS     = 30   # 이메일 발송 최대 건수 제한

NEGATIVE_HINTS = [   # AI 호출 전 룰필터 - 하나라도 있으면 Claude 분석 진행
    # 감성 표현
    "불만", "먹통", "최악", "탈출", "화남", "짜증", "안됨", "안돼",
    "피해", "피해자", "사기", "민원", "환불", "차단", "거부",
    "왜이럼", "왜이러", "미치겠", "황당", "어이없", "답답",
    "이상해", "이상한", "문제", "느려", "버벅", "튕겨", "튕김",
    # 서비스 불만 구어체
    "안되네", "안되는", "안됩니다", "기다리고", "기다림",
    "연결안", "상담원", "대기", "처리안",
    # 증권사 서비스 장애/오류 관련
    "오류", "에러", "버그", "장애", "먹힘",
    "HTS", "MTS", "고객센터", "실패",
    "계좌개설", "해외계좌",
    # 강한 부정 표현
    "접속불가", "로그인불가", "주문불가", "체결불가",
]

# ─────────────────────────────────────────
# 광고성 게시글 제외 패턴 (AI 호출 전 제목 기준 사전 차단)
# ─────────────────────────────────────────
# 사기 업체가 증권사 브랜드를 도용해 올리는 광고글 유형:
#   "한국투자증권 사기 피해 회복 전 알아야 할 핵심 체크포인트" 등
# → 한국투자증권 자체 서비스 불만이 아닌 외부 광고이므로 부정여론에서 제외
AD_SKIP_PATTERNS = [
    # 사기 피해 회복 광고 유형 (구체적 복합구만 사용 - 단어 단독 사용 시 오탐 위험)
    "사기 피해 회복", "피해 회복 전", "피해금 회복", "피해 복구",
    "알아야 할 핵심 체크포인트", "피해 회복 체크포인트", "알아야 할 핵심",
    # 법률·상담 유도 광고 유형
    "무료 상담", "카카오톡 문의", "텔레그램 문의",
    "피해 전문", "전문 변호사", "법률 상담",
    # 투자 리딩방 광고 유형
    "리딩방", "투자 리딩", "수익 인증",
]

KST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────
# 로그 유틸
# ─────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}")

# ─────────────────────────────────────────
# 상태 이메일 (발신자 전용: 탐지 없음 / 오류)
# ─────────────────────────────────────────

def send_status_email(status, detail=""):
    """
    발신자(GMAIL_USER)에게만 발송하는 운영 상태 알림
    status: "no_result" | "error"
    """
    now_kst = datetime.now(KST)
    now_str = now_kst.strftime("%Y.%m.%d %H:%M")

    if status == "no_result":
        subject = f"⚠️[부정여론 탐지] {now_kst.strftime('%m')}월 {now_kst.strftime('%d')}일 {now_kst.strftime('%H')}시 기준 | 탐지 없음"
        body_txt = f"정상 실행되었으나 부정 탐지 게시글이 없습니다.\n\n실행 시각: {now_str} KST"
        color    = "#2e7d32"
        title    = "탐지 없음 - 정상 실행"
    elif status == "warning":
        subject  = f"[부정여론 탐지] ⚠️ 쿠키 만료 임박 | {now_kst.strftime('%m')}월 {now_kst.strftime('%d')}일"
        body_txt = f"쿠키 만료가 임박했습니다. 조치가 필요합니다.\n\n{detail}"
        color    = "#e65100"
        title    = "⚠️ 쿠키 만료 임박 - 재등록 필요"
    else:
        subject  = f"⚠️[부정여론 탐지] {now_kst.strftime('%m')}월 {now_kst.strftime('%d')}일 {now_kst.strftime('%H')}시 기준 | 오류"
        body_txt = f"오류가 발생했습니다.\n\n{detail}"
        color    = "#b71c1c"
        title    = "실행 오류 발생"

    html_body = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<!--[if mso]>
<style>body,table,td{{font-family:'Malgun Gothic',sans-serif!important;}}</style>
<![endif]-->
</head>
<body style="margin:0;padding:0;background:#f0f2f5;
             font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f2f5;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="600" cellpadding="0" cellspacing="0" border="0"
           style="max-width:600px;background:#fff;border:1px solid #d5d9e0;">
      <tr>
        <td style="background:{color};padding:20px 28px;">
          <p style="margin:0 0 4px 0;font-size:16px;font-weight:bold;color:#fff;">{title}</p>
          <p style="margin:0;font-size:11px;color:rgba(255,255,255,0.7);">
            네이버 카페 부정여론 탐지봇 &nbsp;&middot;&nbsp; {now_str} KST
          </p>
        </td>
      </tr>
      <tr>
        <td style="padding:24px 28px;font-size:13px;color:#555;line-height:1.8;">
          <pre style="font-family:inherit;white-space:pre-wrap;margin:0;">{detail if detail else body_txt}</pre>
        </td>
      </tr>
      <tr>
        <td style="background:#f8f8f8;border-top:1px solid #ececec;
                   padding:12px 20px;text-align:center;
                   font-size:11px;color:#aaa;line-height:1.8;">
          탐지 키워드 : 한국투자증권, 한투, 뱅키스<br>
          담당자 : 최진후 차장
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"⚠️ eBiz 부정여론봇 <{GMAIL_USER}>"
    msg["To"]      = GMAIL_USER
    msg["Date"]    = now_kst.strftime("%a, %d %b %Y %H:%M:%S +0900")
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PW)
        s.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())
    log(f"상태 이메일 발송 ({status}) → {GMAIL_USER}")

# ─────────────────────────────────────────
# DB (중복 방지)
# ─────────────────────────────────────────

_db_cache = None  # 런타임 메모리 캐시 (파일 I/O 최소화)

def init_db():
    global _db_cache
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    _db_cache = _load_db()  # 시작 시 1회 로드

def _load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_new(unique_id):
    global _db_cache
    if _db_cache is None:
        _db_cache = _load_db()
    return unique_id not in _db_cache

def mark_seen(unique_id):
    global _db_cache
    if _db_cache is None:
        _db_cache = _load_db()
    _db_cache[unique_id] = datetime.now(KST).isoformat()  # KST로 저장
    _save_db(_db_cache)  # 즉시 파일 동기화 (중간 중단 대비)

def cleanup_db(days=30):
    """30일 지난 항목 자동 삭제"""
    global _db_cache
    data = _db_cache if _db_cache is not None else _load_db()
    cutoff = datetime.now(KST) - timedelta(days=days)
    cleaned = {}
    for k, v in data.items():
        try:
            dt = datetime.fromisoformat(v)
            # naive면 KST로 간주
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            if dt > cutoff:
                cleaned[k] = v
        except Exception:
            pass  # 파싱 불가 항목 제거
    if len(cleaned) < len(data):
        _db_cache = cleaned
        _save_db(cleaned)
        log(f"DB 정리: {len(data) - len(cleaned)}건 삭제 ({len(cleaned)}건 유지)")

def parse_date(date_str):
    """날짜 문자열을 KST aware datetime으로 변환 (GitHub Actions UTC 서버 환경 대응)"""
    now = datetime.now(KST)
    try:
        if date_str == "방금":
            return now
        if date_str == "어제":
            return now - timedelta(days=1)
        if "분 전" in date_str:
            return now - timedelta(minutes=int(date_str.replace("분 전", "").strip()))
        if "시간 전" in date_str:
            return now - timedelta(hours=int(date_str.replace("시간 전", "").strip()))
        if ":" in date_str:
            t = datetime.strptime(date_str, "%H:%M")
            # 네이버 f-e 검색 목록의 HH:MM은 UTC+9가 이중 적용된 값으로 내려옴
            # (실제 KST 06:53인데 15:53으로 표시됨) → 9시간 빼서 보정
            parsed = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            parsed = parsed - timedelta(hours=9)
            # 미래 시각이면 전날로 처리
            if parsed > now:
                parsed -= timedelta(days=1)
            return parsed
        if "." in date_str:
            clean = date_str.strip().rstrip(".")
            d = datetime.strptime(clean, "%Y.%m.%d")
            return d.replace(hour=0, minute=0, second=0, tzinfo=KST)
    except Exception:
        pass
    return now

def search_keyword(page, cafe_id, num_id, keyword):
    """카페 인카페 검색으로 키워드 포함 게시글 수집 (24시간 이내)"""
    encoded = urllib.parse.quote(keyword)
    # 가계부/절약 카페는 제목 전용 검색으로 오탐 방지
    if cafe_id in ["onepieceholicplus"]:
        url = f"https://cafe.naver.com/f-e/cafes/{num_id}/menus/0?viewType=L&ta=TITLE&page=1&q={encoded}"
    else:
        url = f"https://cafe.naver.com/f-e/cafes/{num_id}/menus/0?viewType=L&ta=ARTICLE_COMMENT&page=1&q={encoded}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        log(f"  페이지 로드 타임아웃, 재시도: {e}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(random.randint(3000, 6000))

    log(f"  검색: [{keyword}] → {url[:80]}")

    cutoff = datetime.now(KST) - timedelta(hours=TIME_WINDOW)
    posts  = []

    rows = []
    for sel in ["table.article-table tbody tr", ".article-board tbody tr", ".ArticleItem"]:
        all_rows = page.query_selector_all(sel)
        if all_rows:
            rows = [r for r in all_rows if "board-notice" not in (r.get_attribute("class") or "")]
            break
    log(f"  검색 결과 {len(rows)}건")

    for row in rows:
        try:
            title_el = row.query_selector("a.article")
            if not title_el:
                continue

            title = title_el.inner_text().strip()
            if not title:
                continue

            href = title_el.get_attribute("href") or ""
            if "/articles/" in href:
                post_id = href.split("/articles/")[-1].split("?")[0]
            elif "articleid=" in href:
                post_id = href.split("articleid=")[-1].split("&")[0]
            else:
                post_id = href.rstrip("/").split("/")[-1].split("?")[0]

            if not post_id or not post_id.isdigit():
                continue

            date_str = ""
            all_tds = row.query_selector_all("td")
            if len(all_tds) >= 4:
                candidate = all_tds[3].inner_text().strip().split("\n")[0].strip()
                if re.match(r"^(\d{1,2}:\d{2}|\d{4}\.\d{2}\.\d{2}\.?|\d+분 전|\d+시간 전|방금|어제)$", candidate):
                    date_str = candidate.rstrip(".")
            if not date_str:
                for td in all_tds:
                    candidate = td.inner_text().strip().split("\n")[0].strip()
                    if re.match(r"^(\d{1,2}:\d{2}|\d{4}\.\d{2}\.\d{2}\.?|\d+분 전|\d+시간 전|방금|어제)$", candidate):
                        date_str = candidate.rstrip(".")
                        break

            if not date_str:
                post_time = datetime.now(KST)
            else:
                post_time = parse_date(date_str)

            if post_time < cutoff:
                continue

            posts.append({
                "post_id":   post_id,
                "title":     title,
                "url":       f"https://cafe.naver.com/f-e/cafes/{num_id}/articles/{post_id}",
                "post_time": post_time,
                "date_str":  date_str,
                "keyword":   keyword,
            })
        except Exception as e:
            log(f"  파싱 오류: {e}")

    return posts


def get_post_list(page, cafe_id, num_id):
    """키워드별 검색으로 게시글 수집 (전체 목록 스캔 불필요)"""
    all_posts = {}

    for keyword in KEYWORDS:
        results = search_keyword(page, cafe_id, num_id, keyword)
        for p in results:
            pid = p["post_id"]
            if pid not in all_posts:
                all_posts[pid] = p
            else:
                existing_kw = all_posts[pid].get("keyword", "")
                if keyword not in existing_kw:
                    all_posts[pid]["keyword"] = existing_kw + ", " + keyword

    posts = list(all_posts.values())
    log(f"키워드 검색 완료: {len(posts)}건 수집 (중복 제거)")
    return posts

# ─────────────────────────────────────────
# 게시글 본문 수집
# ─────────────────────────────────────────

def get_post_detail(page, post_url, cafe_id):
    """본문 텍스트 반환 - f-e URL로 접근 후 iframe에서 본문 파싱"""
    BODY_SELECTORS = [
        ".se-main-container",
        ".ArticleContentBox",
        ".article-viewer",
        ".ContentRenderer",
        "#tbody",
        ".article_body",
        ".se-component-content",
    ]

    try:
        page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(random.randint(2000, 3500))
    except Exception as e:
        log(f"  본문 페이지 로드 실패 (제목만으로 분석 진행): {e}")
        page.wait_for_timeout(1000)

    body = ""

    try:
        for frame in page.frames:
            if "ca-fe" in frame.url or "articles" in frame.url:
                for sel in BODY_SELECTORS:
                    el = frame.query_selector(sel)
                    if el:
                        body = el.inner_text().strip()[:2000]
                        if body:
                            break
                if body:
                    break
    except Exception as e:
        log(f"iframe 접근 오류: {e}")

    if not body:
        try:
            for sel in BODY_SELECTORS:
                el = page.query_selector(sel)
                if el:
                    body = el.inner_text().strip()[:2000]
                    if body:
                        break
        except Exception as e:
            log(f"본문 직접 파싱 오류: {e}")

    if not body:
        log("본문 비어 있음 - 제목만으로 AI 분석 진행")

    return body

# ─────────────────────────────────────────
# Claude AI 감성 분석
# ─────────────────────────────────────────

def analyze_sentiment(title, body, keyword):
    prompt = f"""다음은 네이버 카페 게시글입니다.
이 게시글이 '한국투자증권(한투/뱅키스/BanKIS)' 자체에 대한 불만·비판·리스크인지 판단하고, 3줄 이내로 요약해주세요.

[제목]
{title}

[본문]
{body[:1500] if body else "(본문 없음 - 제목만으로 판단)"}

▶ 부정(is_negative: true)으로 판단할 것:
- 한국투자증권의 서비스·앱·장애·응대·수수료·전산·출금·주문·HTS/MTS·신뢰와 직접 관련된 불만·비판·피해
- 욕설·비방·사기 주장·집단 민원 촉구

▶ 부정 아님(is_negative: false)으로 판단할 것:
- 단순 질문·이벤트 문의·정보 공유·증권사 비교
- 시장 전반 불만 또는 타 증권사 불만
- 한국투자증권 단순 언급 (중립·긍정 문맥)
- 일반 투자 손실 (한국투자증권 귀책 아닌 경우)
- 영웅문·키움·미래에셋·삼성증권·NH·신한·KB 등 타 증권사 앱·서비스 문제 (댓글에 한투가 단순 언급된 경우 포함)
- 광고성·홍보성 게시글 (사기 피해 회복 안내, 법률 상담 유도, 무료 상담 홍보 등) → score=0
- 한국투자증권 브랜드를 사칭하는 사기꾼 주의 안내글 (한투 자체 서비스 문제 아님) → score=0

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{{"is_negative": true or false, "summary": "게시글 내용을 3줄 이내 요약 (객관적 서술체)", "score": 0~10, "reply": "추천 대응 답변"}}
score는 부정 강도 (0=전혀 부정 아님, 10=매우 부정적)
reply는 같은 카페 회원이 친근하게 댓글 달아주는 느낌. 2~3문장. 편한 존댓말. 핵심만 짧게. 반드시 지킬 규칙: (1) 고객센터 번호가 필요하면 반드시 "1544-5000"만 사용. 다른 번호는 절대 만들어내지 말 것 (2) URL·구체적 수치·정책 등 확인 안 된 내용은 단정하지 말고 "정확한 건 앱이나 고객센터에서 확인해보세요" 수준으로 마무리 (3) 불만글이면 가볍게 공감 한 마디 + 앱/고객센터 확인 안내 (4) 뉴스 공유·사건 사고 글은 "저도 봤는데 좀 당황스럽네요" 같은 가벼운 반응 수준으로, 금감원·보상 등 극단적 표현 금지 (5) 질문글이면 아는 선에서 짧게 + 불확실하면 "정확한 건 직접 확인해보시는 게 나을 것 같아요" (6) 대응 불필요한 경우 그 이유 한 줄."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        response.raise_for_status()
        resp = response.json()
        if "error" in resp:
            err_type = resp["error"].get("type", "")
            if err_type == "overloaded_error":
                log("AI Overloaded - 10초 후 재시도")
                time.sleep(10)
                response = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": CLAUDE_MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
                    timeout=30,
                )
                response.raise_for_status()
                resp = response.json()
                if "error" in resp:
                    log(f"AI 재시도 실패: {resp['error'].get('message','')}")
                    return {"is_negative": False, "summary": "분석 실패", "score": 0, "reply": ""}
            else:
                log(f"AI API 오류: {resp['error'].get('message','')}")
                return {"is_negative": False, "summary": "분석 실패", "score": 0, "reply": ""}
        text = resp["content"][0]["text"].strip()
        match = re.search(r'\{.*\}', text, re.S)
        if not match:
            raise ValueError("JSON 응답 없음")
        result = json.loads(match.group())
        # score 타입 정규화 (AI가 문자열·float·null 반환 시 TypeError 방지)
        try:
            result["score"] = int(float(result.get("score") or 0))
        except (ValueError, TypeError):
            result["score"] = 0
        # AI가 만들어낸 전화번호 할루시네이션 방지
        if "reply" in result and result["reply"]:
            phone_pattern = re.compile(r"(\(?)((?:\d{2,4}[-.])?\d{3,4}[-.]\d{4})(\)?)")
            def fix_phone(m):
                num = re.sub(r"[^0-9]", "", m.group(2))
                if num == "15445000":
                    return m.group()
                return f"{m.group(1)}1544-5000{m.group(3)}"
            result["reply"] = phone_pattern.sub(fix_phone, result["reply"])
        return result
    except Exception as e:
        log(f"AI 분석 오류: {e}")
        return {"is_negative": False, "summary": "분석 실패", "score": 0, "reply": ""}

# ─────────────────────────────────────────
# 이메일 발송 (담당자용)
# ─────────────────────────────────────────

def build_card(post, idx, total):
    title    = html.escape(post["title"])
    url      = html.escape(post["url"], quote=True)
    keyword  = html.escape(post["matched_kw"])
    score    = post["score"]
    summary  = html.escape(post["summary"])
    reply    = html.escape(post.get("reply", ""))

    related_posts = post.get("related_posts", [])
    common_kws    = post.get("common_keywords", [])
    if related_posts:
        kw_str = " · ".join(common_kws) if common_kws else ""
        related_items = "".join(
            f'<tr><td style="padding:3px 0;font-size:12px;color:#555;">'
            f'· <a href="{html.escape(p["url"],quote=True)}" style="color:#555;text-decoration:none;">'
            f'{html.escape(p["title"][:40])}...</a>'
            f' <span style="color:#bbb;font-size:11px;">{p.get("cafe_name","")}</span></td></tr>'
            for p in related_posts
        )
        related_html = f'''<table width="100%" cellpadding="0" cellspacing="0" border="0"
          style="margin-top:8px;background:#fff8e1;border-left:3px solid #ffc107;">
          <tr><td style="padding:8px 12px;">
            <p style="margin:0 0 5px 0;font-size:10px;font-weight:bold;color:#f57f17;">
              📎 유사 이슈 {len(related_posts)}건 추가 탐지 {f"({kw_str})" if kw_str else ""}</p>
            <table cellpadding="0" cellspacing="0">{related_items}</table>
          </td></tr></table>'''
    else:
        related_html = ""

    post_dt  = post.get("post_time")
    raw_date = post.get("date_str", "")
    if post_dt:
        if post_dt.tzinfo is not None:
            dt_kst = post_dt.astimezone(KST)
        else:
            dt_kst = post_dt.replace(tzinfo=KST)
        date_str = dt_kst.strftime("%Y.%m.%d %H:%M")
    elif raw_date:
        date_str = raw_date.rstrip(".")
    else:
        date_str = "-"

    return f"""
    <!--[if true]><table width="100%" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
    <table width="100%" cellpadding="0" cellspacing="0" border="0"
           style="border-bottom:1px solid #f0f0f0;">
      <tr>
        <td style="padding:20px 28px;">

          <p style="margin:0 0 8px 0;font-size:10px;font-weight:bold;
                    color:#c62828;letter-spacing:0.6px;">게시글 {idx} / {total} &nbsp;·&nbsp; {post.get('cafe_name','')}</p>

          <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:10px;">
            <tr>
              <td style="background:#fdecea;padding:3px 10px;
                         font-size:11px;font-weight:bold;color:#c62828;">
                탐지 키워드 : {keyword}
              </td>
              <td width="6"></td>
              <td style="background:#fdecea;padding:3px 10px;
                         font-size:11px;font-weight:bold;color:#c62828;">
                부정 강도 : {score}/10
              </td>
            </tr>
          </table>

          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="margin-bottom:12px;">
            <tr>
              <td style="vertical-align:top;">
                <a href="{url}" style="text-decoration:none;color:#111;">
                  <span style="font-size:15px;font-weight:bold;line-height:1.5;">{title}</span>
                </a>
                <span style="font-size:11px;color:#bbb;margin-left:6px;">{date_str}</span>
              </td>
              <td width="60" style="vertical-align:top;text-align:right;padding-top:2px;">
                <a href="{url}"
                   style="font-size:10px;color:#c62828;font-weight:bold;
                          border:1px solid #c62828;padding:2px 6px;
                          text-decoration:none;">↗</a>
              </td>
            </tr>
          </table>

          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="margin-bottom:14px;background:#fafafa;">
            <tr>
              <td width="3" style="background:#e0e0e0;font-size:0;">&nbsp;</td>
              <td style="padding:12px 14px;">
                <p style="margin:0 0 5px 0;font-size:10px;font-weight:bold;
                          color:#c62828;letter-spacing:0.6px;">✦ AI 요약</p>
                <p style="margin:0;font-size:13px;color:#555;line-height:1.8;">{summary}</p>
              </td>
            </tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;background:#f0f4ff;">
            <tr>
              <td width="3" style="background:#3d5afe;font-size:0;">&nbsp;</td>
              <td style="padding:10px 14px;">
                <p style="margin:0 0 4px 0;font-size:10px;font-weight:bold;color:#3d5afe;letter-spacing:0.6px;">💬 AI 추천 대응</p>
                <p style="margin:0;font-size:13px;color:#444;line-height:1.8;">{reply}</p>
              </td>
            </tr>
          </table>
          {related_html}

        </td>
      </tr>
    </table>
    <!--[if true]></td></tr></table><![endif]-->"""


def group_similar_alerts(alert_posts):
    """Claude AI가 유사 이슈 여부를 판단해서 묶음 처리"""
    if len(alert_posts) <= 1:
        return alert_posts, []

    titles_text = "\n".join(
        f"{i}. [{p['cafe_name']}] {p['title']}" for i, p in enumerate(alert_posts)
    )
    prompt = f"""아래는 네이버 카페에서 탐지된 한국투자증권 관련 게시글 목록입니다.
같은 이슈(동일한 서비스 문제, 장애, 불만)를 다루는 글끼리 묶어주세요.

{titles_text}

규칙:
- 같은 앱 오류/장애면 같은 그룹
- 같은 기능 문의면 같은 그룹
- 명확히 다른 주제면 별도 그룹
- 단독이면 자기 자신만 포함

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{{"groups": [[0, 1], [2], [3, 4]]}}
groups는 인덱스 리스트의 리스트. 모든 인덱스가 정확히 한 번씩 포함되어야 함."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        response.raise_for_status()
        resp = response.json()
        if "error" in resp:
            raise ValueError(resp["error"])
        text = resp["content"][0]["text"].strip()
        match = re.search(r'\{.*\}', text, re.S)
        if not match:
            raise ValueError("JSON 없음")
        data = json.loads(match.group())
        groups_idx = data.get("groups", [])

        all_idx = [i for g in groups_idx for i in g]
        if sorted(all_idx) != list(range(len(alert_posts))):
            raise ValueError("인덱스 불일치")

    except Exception as e:
        log(f"이슈 묶음 AI 판단 실패 ({e}) - 개별 처리")
        return alert_posts, []

    flat_posts = []
    for group in groups_idx:
        if len(group) == 1:
            flat_posts.append(alert_posts[group[0]])
        else:
            members = [alert_posts[i] for i in group]
            rep = max(members, key=lambda x: x.get("score", 0))
            related = [p for p in members if p is not rep]
            rep["related_posts"] = related
            rep["common_keywords"] = []
            flat_posts.append(rep)
            log(f"유사 이슈 묶음: {[alert_posts[i]['title'][:20] for i in group]}")

    return flat_posts, []


def send_alert_batch(alert_posts, crawled_count, keyword_count):
    """탐지 게시글 담당자 이메일 발송 (아웃룩/Gmail/모바일 호환)"""
    total    = len(alert_posts)
    now_kst  = datetime.now(KST)
    now_str  = now_kst.strftime("%Y.%m.%d %H:%M")
    subject  = f"⚠️[부정여론 탐지] {now_kst.strftime('%m')}월 {now_kst.strftime('%d')}일 {now_kst.strftime('%H')}시 기준"
    banner_badge = f"AI 부정여론 탐지 · {total}건"
    banner_title = f"네이버 카페 부정 언급 {total}건 탐지"

    sorted_posts = alert_posts  # main()에서 이미 score 내림차순 정렬됨
    cards_html = "".join(build_card(p, i+1, total) for i, p in enumerate(sorted_posts))

    html_body = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<!--[if mso]>
<style type="text/css">
  body, table, td {{ font-family: 'Malgun Gothic', sans-serif !important; }}
</style>
<![endif]-->
</head>
<body style="margin:0;padding:0;background:#f0f2f5;
             font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f2f5;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="600" cellpadding="0" cellspacing="0" border="0"
           style="max-width:600px;background:#fff;border:1px solid #d5d9e0;">
      <tr>
        <td style="background:#c62828;padding:20px 28px;">
          <p style="margin:0 0 6px 0;font-size:10px;font-weight:bold;
                    color:#ffcdd2;letter-spacing:0.8px;">{banner_badge}</p>
          <p style="margin:0 0 8px 0;font-size:18px;font-weight:bold;
                    color:#fff;line-height:1.3;">{banner_title}</p>
          <table cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="font-size:10px;color:rgba(255,255,255,0.6);padding-right:4px;">크롤링</td>
              <td style="font-size:12px;font-weight:bold;color:#fff;padding-right:12px;">{crawled_count}건</td>
              <td style="font-size:10px;color:rgba(255,255,255,0.4);padding-right:12px;">▶</td>
              <td style="font-size:10px;color:rgba(255,255,255,0.6);padding-right:4px;">키워드 탐지</td>
              <td style="font-size:12px;font-weight:bold;color:#fff;padding-right:12px;">{keyword_count}건</td>
              <td style="font-size:10px;color:rgba(255,255,255,0.4);padding-right:12px;">▶</td>
              <td style="font-size:10px;color:rgba(255,255,255,0.6);padding-right:4px;">AI 필터링</td>
              <td style="font-size:12px;font-weight:bold;color:#ffcdd2;">{total}건</td>
            </tr>
          </table>
          <p style="margin:6px 0 0;font-size:11px;color:rgba(255,255,255,0.5);">
            {now_str} KST &nbsp;&middot;&nbsp; Claude AI 분석
          </p>
        </td>
      </tr>
      <tr><td style="padding:0;">{cards_html}</td></tr>
      <tr>
        <td style="background:#f8f8f8;border-top:1px solid #ececec;
                   padding:12px 20px;text-align:center;
                   font-size:11px;color:#aaa;line-height:1.8;">
          탐지 키워드 : 한국투자증권, 한투, 뱅키스<br>
          담당자 : 최진후 차장<br>
          <span style="color:#ccc;">Powered by Claude AI · 자동 수집 및 감성 분석</span>
          <br><br>
          <table cellpadding="0" cellspacing="0" border="0" style="margin:8px auto 0;border-collapse:collapse;">
            <tr>
              <td style="font-size:9px;color:#999;padding:0 0 4px 0;text-align:left;letter-spacing:0.5px;">
                ■ 부정 강도 기준 (Claude AI 채점)
              </td>
            </tr>
            <tr>
              <td>
                <table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
                  <tr>
                    <td style="background:#4caf50;width:40px;height:8px;font-size:0;">&nbsp;</td>
                    <td style="background:#8bc34a;width:40px;height:8px;font-size:0;">&nbsp;</td>
                    <td style="background:#ffc107;width:40px;height:8px;font-size:0;">&nbsp;</td>
                    <td style="background:#ff9800;width:40px;height:8px;font-size:0;">&nbsp;</td>
                    <td style="background:#f44336;width:40px;height:8px;font-size:0;">&nbsp;</td>
                  </tr>
                  <tr>
                    <td style="font-size:8px;color:#aaa;text-align:center;padding-top:3px;">0~1<br>무관</td>
                    <td style="font-size:8px;color:#aaa;text-align:center;padding-top:3px;">2~3<br>경미</td>
                    <td style="font-size:8px;color:#aaa;text-align:center;padding-top:3px;">4~5<br>불만</td>
                    <td style="font-size:8px;color:#aaa;text-align:center;padding-top:3px;">6~7<br>비판</td>
                    <td style="font-size:8px;color:#aaa;text-align:center;padding-top:3px;">8~10<br>긴급</td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"⚠️ eBiz 부정여론봇 <{GMAIL_USER}>"
    msg["To"]      = ", ".join(RECIPIENTS)
    if CC_RECIPIENTS:
        msg["Cc"]  = ", ".join(CC_RECIPIENTS)
    msg["Date"]    = now_kst.strftime("%a, %d %b %Y %H:%M:%S +0900")
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    all_recipients = RECIPIENTS + CC_RECIPIENTS
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PW)
        s.sendmail(GMAIL_USER, all_recipients, msg.as_string())
    log(f"담당자 이메일 발송 완료 - {total}건")

# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────

def main():
    init_db()
    cleanup_db()
    log("=" * 48)
    log(f"모니터링 시작 | {len(CAFES)}개 카페")
    log(f"탐지 키워드: {', '.join(KEYWORDS)}")
    log(f"부정 강도 기준: {SCORE_THRESHOLD} 이상 알림 발송")
    log("=" * 48)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            if not os.path.exists(COOKIE_FILE):
                raise RuntimeError("naver_cookies.json 없음 - NAVER_COOKIES_JSON Secret 확인 필요")

            log("쿠키 파일로 접속")

            # 쿠키 만료 사전 감지
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookie_data = json.load(f)
                cookies = cookie_data.get("cookies", [])
                now_ts = datetime.now(KST).timestamp()
                expiring_soon = []
                for c in cookies:
                    exp = c.get("expires", -1)
                    if 0 < exp < now_ts + (3 * 24 * 3600):
                        expiring_soon.append(c.get("name", "unknown"))
                if expiring_soon:
                    send_status_email("warning",
                        detail=f"쿠키 만료 임박 (3일 이내): {', '.join(expiring_soon)}\n"
                               f"NAVER_COOKIES_JSON Secret 재등록을 준비해주세요.")
                    log(f"⚠️ 쿠키 만료 임박: {expiring_soon}")
            except Exception as e:
                log(f"쿠키 만료 확인 중 오류: {e}")

            context = browser.new_context(storage_state=COOKIE_FILE)
            page    = context.new_page()

            page.goto("https://www.naver.com", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            if "nid.naver.com" in page.url:
                raise RuntimeError("쿠키 만료 - NAVER_COOKIES_JSON Secret 재등록 필요")
            log("로그인 상태 확인 완료")

            total_crawled  = 0
            total_keywords = 0
            all_alerts     = []

            for cafe in CAFES:
                cafe_id   = cafe["id"]
                num_id    = cafe["num_id"]
                cafe_name = cafe["name"]
                log(f"\n── {cafe_name} ({cafe_id}) ──")

                try:
                    # STEP 1: 목록 수집
                    posts = get_post_list(page, cafe_id, num_id)

                    # STEP 2: 신규 여부 확인 (중복 제거)
                    new_posts = [p for p in posts if is_new(f"{cafe_id}:{p['post_id']}")]
                    log(f"신규 게시글: {len(new_posts)}건")
                    total_crawled += len(new_posts)

                    # STEP 3: 본문 수집 + AI 분석
                    for post in new_posts:

                        matched = post.get("keyword") or next(
                            (kw for kw in KEYWORDS if kw.lower() in post["title"].lower()), None
                        )
                        if not matched:
                            mark_seen(f"{cafe_id}:{post['post_id']}")
                            continue

                        total_keywords += 1
                        log(f"키워드 [{matched}] 탐지: {post['title'][:40]}...")

                        # 본문 수집
                        body = get_post_detail(page, post["url"], cafe_id)

                        # AI 호출 전 룰필터 ①: 부정 힌트 없으면 Claude 미호출
                        combined_text = (post["title"] + " " + body).lower()
                        has_hint = any(hint in combined_text for hint in NEGATIVE_HINTS)

                        if not has_hint:
                            log(f"  룰필터 통과 - AI 분석 생략 (부정 힌트 없음)")
                            mark_seen(f"{cafe_id}:{post['post_id']}")
                            continue

                        # AI 호출 전 룰필터 ②: 광고성 게시글 제목 패턴 차단
                        is_ad = any(pat in post["title"] for pat in AD_SKIP_PATTERNS)
                        if is_ad:
                            log(f"  광고성 게시글 제외: {post['title'][:40]}")
                            mark_seen(f"{cafe_id}:{post['post_id']}")
                            continue

                        result = analyze_sentiment(post["title"], body, matched)
                        if result["summary"] != "분석 실패":
                            mark_seen(f"{cafe_id}:{post['post_id']}")
                        log(f"AI 결과 - 부정:{result['is_negative']} | 강도:{result['score']}/10")

                        post["cafe_name"]  = cafe_name
                        post["matched_kw"] = matched
                        post["score"]      = result["score"]
                        post["summary"]    = result["summary"]
                        post["reply"]      = result.get("reply", "")
                        if result["score"] >= SCORE_THRESHOLD:
                            all_alerts.append(post)

                except Exception as cafe_err:
                    log(f"  ⚠️ [{cafe_name}] 오류 - 다음 카페로 계속: {cafe_err}")
                    try:
                        page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    continue

            context.close()
            browser.close()

            # STEP 4: 결과에 따라 이메일 분기
            log(f"\n크롤링 {total_crawled}건 → 키워드 탐지 {total_keywords}건 → AI 필터링 {len(all_alerts)}건")
            if all_alerts:
                all_alerts = sorted(all_alerts, key=lambda x: x.get("score", 0), reverse=True)
                if len(all_alerts) > MAX_ALERTS:
                    log(f"알림 건수 초과 — {len(all_alerts)}건 중 {MAX_ALERTS}건만 발송")
                    all_alerts = all_alerts[:MAX_ALERTS]
                all_alerts, _ = group_similar_alerts(all_alerts)
                all_alerts = sorted(all_alerts, key=lambda x: x.get("score", 0), reverse=True)
                grouped_cnt = sum(1 for p in all_alerts if p.get("related_posts"))
                if grouped_cnt:
                    log(f"유사 이슈 묶음: {grouped_cnt}건 그룹화")
                send_alert_batch(
                    all_alerts,
                    crawled_count=total_crawled,
                    keyword_count=total_keywords,
                )
            else:
                send_status_email("no_result")

    except Exception as e:
        err = traceback.format_exc()
        log(f"오류 발생: {e}")
        send_status_email("error", detail=err)
        raise

    log("모니터링 완료")

if __name__ == "__main__":
    main()

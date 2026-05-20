"""
네이버 뉴스 키워드 모니터링 & Claude AI 필터링 & 이메일 알림
Railway Cron 버전 / 네이버 검색 API 사용
"""

import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import csv
import time
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime as _pdt

# ─────────────────────────────────────────────
# Railway Volume 경로 설정
# Railway 대시보드에서 /data 로 마운트
# ─────────────────────────────────────────────
VOLUME_DIR    = os.environ.get("VOLUME_DIR", "/data")
SEEN_FILE     = os.path.join(VOLUME_DIR, "seen_news.json")
EXPOSURE_FILE = os.path.join(VOLUME_DIR, "exposure_data.csv")
DATA_DIR      = os.path.join(VOLUME_DIR, "data")

# Volume 디렉토리 없으면 생성 (로컬 테스트용)
os.makedirs(VOLUME_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ─────────────────────────────────────────────
# 설정 — Railway Variables에서 자동으로 읽어옴
# ─────────────────────────────────────────────
EMAIL_SENDER       = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD     = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVERS    = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",")]
NO_RESULT_RECEIVER = os.environ.get("NO_RESULT_RECEIVER", "").strip()
ANTHROPIC_KEY      = os.environ["ANTHROPIC_API_KEY"]
NAVER_CLIENT_ID    = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["부실 리스크", "신용 리스크", "유동성 리스크", "디폴트 리스크", "기업회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지", "반대매매 급증", "신용등급 강등", "PF 부실", "미매각", "신용융자", "발행어음", "서킷브레이커"]
MAX_NEWS_PER_KEYWORD = 1000


def load_exposure_data() -> dict:
    if not os.path.exists(EXPOSURE_FILE):
        # Volume에 없으면 레포 루트에서 복사 시도
        local_csv = os.path.join(os.path.dirname(__file__), "exposure_data.csv")
        if os.path.exists(local_csv):
            import shutil
            shutil.copy(local_csv, EXPOSURE_FILE)
            print(f"  exposure_data.csv → {EXPOSURE_FILE} 복사 완료")
        else:
            return {}
    try:
        with open(EXPOSURE_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            result = {}
            for row in reader:
                name = row.get("종목명", "").strip()
                if name:
                    result[name] = row
            return result
    except Exception:
        return {}


def find_exposure(entity: str, exposure_data: dict) -> list:
    import re
    if not entity or not exposure_data:
        return []
    if entity in exposure_data:
        return [exposure_data[entity]]
    results = []
    entity_pattern = re.compile(
        r'(?<![가-힣a-zA-Z0-9])' + re.escape(entity) + r'(?![가-힣a-zA-Z0-9])'
    )
    for name, row in exposure_data.items():
        name_pattern = re.compile(
            r'(?<![가-힣a-zA-Z0-9])' + re.escape(name) + r'(?![가-힣a-zA-Z0-9])'
        )
        if entity_pattern.search(name) or name_pattern.search(entity):
            results.append(row)
    return results


def load_competitor_notices() -> list:
    CREDIT_KEYWORDS = [
        "신용한도", "신용융자", "신용공여", "신용거래",
        "증거금률", "증거금 변경", "반대매매 급증",
        "대출한도", "신용대출", "신용 중단", "한도 축소",
        "신용 재개", "신용거래 제한"
    ]
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_dates = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(2)}
    result = []
    if not os.path.exists(DATA_DIR):
        return []
    try:
        import csv as _csv
        for fname in os.listdir(DATA_DIR):
            if not fname.endswith(".csv") or fname == "broker_notices_merged.csv":
                continue
            fpath = os.path.join(DATA_DIR, fname)
            with open(fpath, "r", encoding="utf-8-sig") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    date = row.get("date", "")
                    title = row.get("title", "")
                    company = row.get("company", "")
                    if date not in valid_dates:
                        continue
                    if any(kw in title for kw in CREDIT_KEYWORDS):
                        result.append({
                            "company": company,
                            "title": title,
                            "date": date,
                            "url": row.get("url", ""),
                        })
    except Exception as e:
        print(f"  경쟁사 공지 로드 오류: {e}")

    seen_keys = set()
    deduped = []
    for item in result:
        key = (item["company"].strip(), item["title"].strip())
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)
    return deduped


def build_summary_html(ai_summary: str) -> str:
    lines = [l.strip() for l in ai_summary.split("\n") if l.strip()]
    rows_html = ""
    current_label = ""
    current_items = []

    def flush_row(label, items):
        if not label:
            return ""
        content_html = "<br>".join(items) if items else ""
        return f"""<tr>
          <td width="80" valign="top" style="padding:7px 10px;font-size:13px;font-weight:bold;color:#3b5491;border-bottom:1px solid #eef2ff;white-space:nowrap;">{label}</td>
          <td style="padding:7px 10px;font-size:13px;color:#334155;line-height:1.7;border-bottom:1px solid #eef2ff;">{content_html}</td>
        </tr>"""

    for line in lines:
        if line.startswith("▸"):
            rows_html += flush_row(current_label, current_items)
            current_label = line.replace("▸", "").strip()
            current_items = []
        elif line.startswith("·") or line.startswith("•"):
            current_items.append(line)
        else:
            current_items.append(line)

    rows_html += flush_row(current_label, current_items)

    return f"""<p style="margin:0 0 10px 0;font-size:15px;font-weight:bold;color:#3b5491;">AI 분석 요약</p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        {rows_html}
      </table>"""


def build_competitor_html(notices: list, today_str: str) -> str:
    if not notices:
        return ""
    rows_html = ""
    for i, n in enumerate(notices):
        border = "border-bottom:1px solid #dce8ff;" if i < len(notices) - 1 else ""
        url = n.get('url', '')
        title_cell = f'<a href="{url}" style="color:#334155;text-decoration:none;">{n["title"]}</a>' if url else n['title']
        rows_html += f"""<tr>
          <td width="100" valign="middle" style="padding:7px 4px;font-size:13px;font-weight:bold;color:#1e293b;{border}">{n['company']}</td>
          <td valign="middle" style="padding:7px 4px;font-size:13px;{border}">{title_cell}</td>
          <td align="right" valign="middle" style="padding:7px 4px;font-size:11px;color:#94a3b8;white-space:nowrap;{border}">{n['date'][5:].replace('-', '/')}</td>
        </tr>"""
    return f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f5ff;border-bottom:1px solid #e2e8f0;">
      <tr>
        <td style="padding:14px 22px 4px 22px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td><span style="font-size:15px;font-weight:bold;color:#3b5491;">경쟁사 신용·대출 특이사항</span></td>
              <td align="right"><span style="font-size:11px;color:#94a3b8;">{today_str} 당일 기준</span></td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 22px 14px 22px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            {rows_html}
          </table>
        </td>
      </tr>
    </table>"""


def load_seen_urls() -> set:
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(2)
    }
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set()
        urls = set()
        for k in valid_keys:
            urls |= set(data.get(k, []))
        return urls
    return set()


def save_seen_urls(seen: set):
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    current_key = now.strftime("%Y-%m-%d %H")
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(2)
    }
    existing = {}
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                existing = {k: v for k, v in raw.items() if k in valid_keys}
        except Exception:
            existing = {}
    existing[current_key] = list(seen)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False)
    print(f"  seen_news.json 저장 완료 → {SEEN_FILE}")


def crawl_naver_news(keyword: str) -> list:
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    cutoff_kst = now_kst - timedelta(hours=1)

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    articles = []
    start = 1

    while True:
        params = {
            "query": keyword,
            "display": 100,
            "start": start,
            "sort": "date",
        }
        for crawl_attempt in range(3):
            try:
                res = requests.get(
                    "https://openapi.naver.com/v1/search/news.json",
                    headers=headers,
                    params=params,
                    timeout=15,
                )
                res.raise_for_status()
                data = res.json()
                break
            except Exception as e:
                if crawl_attempt < 2:
                    time.sleep(5)
                else:
                    data = {"items": []}
                    break

        items = data.get("items", [])
        if not items:
            break

        stop = False
        for item in items:
            pub_date_str = item.get("pubDate", "")
            try:
                pub_dt = _pdt(pub_date_str).astimezone(kst)
            except Exception:
                pub_dt = now_kst

            if pub_dt < cutoff_kst:
                stop = True
                break

            title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
            desc  = BeautifulSoup(item.get("description", ""), "html.parser").get_text()
            link  = item.get("originallink") or item.get("link", "")
            pub   = item.get("pubDate", "")
            if title and link:
                articles.append({
                    "title"  : title,
                    "desc"   : (desc[:80].rsplit(" ", 1)[0] if len(desc) > 80 and " " in desc[:80] else desc[:80]) if desc else "",
                    "url"    : link,
                    "pubDate": pub,
                    "keyword": keyword,
                    "body"   : "",
                })

        total = data.get("total", 0)
        start += 100
        if stop or start > min(total, 300):
            break

    return articles


def fetch_article_body(url: str) -> str:
    try:
        res = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        for selector in ["#dic_area", "#articleBodyContents", ".article-body", "#articeBody", "article"]:
            tag = soup.select_one(selector)
            if tag:
                return tag.get_text(separator=" ", strip=True)[:600]
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20)
        return text[:600]
    except Exception:
        return ""


def ai_filter_batch(batch: list, offset: int = 0) -> list:
    if not batch:
        return []

    numbered = "\n".join([
        f"{i+offset+1}. {a['title']}\n   요약: {a.get('desc','')}"
        for i, a in enumerate(batch)
    ])

    prompt = f"""당신은 한국투자증권 eBiz본부 리스크 담당자입니다.
eBiz본부는 비대면 주식거래(온라인 MTS·HTS)를 핵심 사업으로 하며, 리츠·펀드·발행어음·신용융자·IMA 등 금융상품을 온라인 채널로 판매합니다.
고객 자산 손실, 당사 익스포저 손실, 금융당국 제재, 비대면 거래 시스템 리스크에 특히 민감합니다.
아래 기사가 이 네 가지 중 하나로 이어질 직접적 가능성이 있는지 엄격하게 판단하세요. 가능성이 낮으면 과감히 제외하세요.

[반드시 제외 — relevant: false 조건]
다음 중 하나라도 해당하면 즉시 제외하세요:
- 단순히 "파산", "위기" 등 용어만 언급하는 분석·전망·칼럼·오피니언 기사
- 학술·연구·교육·세미나·보고서·강의 관련 기사
- 해외 사례 기사 (국내 증권사에 직접 영향 없는 것)
- 일반 기업 경영 이슈로 금융권 익스포저가 없는 기사
- 제목에 구체적 기업명·사건 없이 일반론만 언급하는 기사
- 수출 확대·실적 개선·신사업 진출·수상·협약·MOU 등 긍정적 기사
- 주가 상승·목표주가 상향·투자의견 매수 등 호재성 기사
- 기업 성장·투자 유치·흑자 전환·신규 상장 등 호재성 기사
- 제품 출시·마케팅·홍보·이벤트 관련 기사
- 증거금·신용융자 관련 투자 교육·이용 방법·금리 비교 안내 기사
- 공모주·IPO 청약 증거금 관련 기사
- 단순 시황·지수 등락 보도
- "전망", "예상", "가능성", "우려" 등 추측성 표현만 있고 확정된 사건이 없는 기사
- 인터뷰·기고·칼럼 형식의 기사
- 해외 기업·시장 이슈로 국내 증권사 직접 익스포저가 없는 기사
- 리스크 관련 정책·제도 변경 예고 기사
- 리스크가 "안정화", "개선", "해소", "완화" 됐다는 기사
- 충당금 감소·자산건전성 개선·부실 축소 등 리스크 해소 방향 기사
- 보험사·캐피탈·저축은행 등 타 금융업권 기사 (증권사 익스포저 없는 것)
- 제약·바이오·유통·제조업 등 비금융 기업 회생·파산 기사 (증권사 직접 여신·보증·판매 상품 익스포저 없는 것)

[관련 있음 — relevant: true 조건]
위 제외 조건에 해당하지 않고 아래 중 하나라도 해당하면 관련 있음:
- 기업 부도·파산·회생·워크아웃·상장폐지가 확정되었거나 신청·징후 단계인 기사
- 금융당국(금감원·금융위)의 증권사 대상 조사·검사·제재·규제 강화 기사
- 부동산PF, 브릿지론, 미매각채권 관련 손실·부실 기사
- 반대매매 급증, 마진콜, 신용융자 한도 소진·중단 관련 기사
- 서킷브레이커 발동, 종목 증거금률 대폭 상향 등 기사
- 발행어음·IMA 관련 증권사 유동성 위기 기사
- 비대면 주식거래 시스템 장애·해킹·전산 사고 관련 기사
- 특정 기업·업종의 신용등급 강등·부실로 증권사 익스포저 손실 우려 기사
- 증권사가 직접 당사자인 제재·손실·건전성 악화 기사

[등급 기준]
- 긴급: 부도·파산·회생·상폐 확정 또는 신청 / 금융당국 조사·제재 착수 / 증권사 직접 손실 발생 / 서킷브레이커 발동 / 반대매매 급증 현실화 / 100억원 이상 채무 디폴트
- 주의: 회생·부도·상폐 가능성 첫 언급 / 조사·제재 예고 단계 / 신용등급 강등 경고 / PF 부실 징후 미확정 단계
- 참고: 업황 파악에 유용하나 직접 위험은 낮은 것

[중복 기사 처리]
- 동일 사건(기업명+사건유형)을 다른 언론사가 보도한 경우 id 가장 작은 것 1건만 relevant:true

반드시 JSON 배열만 반환하세요. 마크다운 코드블록 없이 순수 JSON만.
형식 예시:
[
  {{"id":1,"relevant":true,"grade":"긴급","reason":"리츠 기초자산 회생신청","action":"해당 리츠 보유 고객 전수 파악, 금일 내 평가손 산출","entity":"제이알글로벌리츠"}},
  {{"id":2,"relevant":false,"grade":null,"reason":null,"action":null,"entity":null}}
]

뉴스 목록:
{numbered}"""

    for attempt in range(3):
        try:
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 4000,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if res.status_code == 429:
                wait = 60 * (attempt + 1)
                time.sleep(wait)
                continue
            res.raise_for_status()
            raw = res.json()["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            start_idx = raw.find("[")
            end_idx = raw.rfind("]") + 1
            if start_idx == -1 or end_idx == 0:
                raise ValueError("JSON 배열을 찾을 수 없음")
            raw = raw[start_idx:end_idx]
            grades = json.loads(raw)
            grade_map = {g["id"]: g for g in grades}
            result = []
            for i, article in enumerate(batch):
                info = grade_map.get(i + offset + 1, {})
                if info.get("relevant") and info.get("grade"):
                    article["grade"] = info["grade"]
                    article["reason"] = info.get("reason", "")
                    article["action"] = info.get("action", "")
                    article["entity"] = info.get("entity", "")
                    result.append(article)
            return result
        except Exception as e:
            print(f"AI 필터링 오류: {e}")
            if attempt < 2:
                time.sleep(30)
                continue
            return []
    return []


def dedup_by_title(articles: list) -> list:
    if not articles:
        return []

    numbered = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])
    prompt = f"""아래 뉴스 제목 목록에서 중복 기사를 제거하세요.

[중복 판단 기준]
1. 동일 기업명 + 동일 사건유형
2. 제목 핵심 내용이 80% 이상 동일
3. 동일 정책·제도 변경을 여러 언론사가 보도한 경우
4. 동일 인물·기업의 동일 사건을 다른 각도로 보도한 경우

중복이면 id가 가장 작은 것 1건만 남기세요.

반드시 JSON 배열만 반환하세요. 마크다운 코드블록 없이 순수 JSON만.
형식: [{{"id": 유지할id}}, ...] — 유지할 기사 id만 포함

뉴스 목록:
{numbered}"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        res.raise_for_status()
        raw = res.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        raw = raw[start_idx:end_idx]
        keep_ids = {item["id"] for item in json.loads(raw)}
        return [a for i, a in enumerate(articles) if (i + 1) in keep_ids]
    except Exception as e:
        print(f"중복 제거 오류: {e} — 중복 제거 생략")
        return articles


def regrade_urgent(articles: list) -> list:
    urgent = [a for a in articles if a.get("grade") == "긴급"]
    others = [a for a in articles if a.get("grade") != "긴급"]

    if len(urgent) <= 3:
        return articles

    numbered = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(urgent)])
    prompt = f"""아래 긴급 리스크 기사들의 중요도를 판단하여 상위 3건만 선별하세요.

우선순위:
1. 부도·파산·회생·상폐 확정 또는 신청
2. 증권사 직접 제재·손실 발생 확정
3. 기초자산(리츠·펀드) 부실·상폐 신청

반드시 JSON 배열만 반환하세요. 형식: [{{"id": 유지할id}}, ...]

뉴스 목록:
{numbered}"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        res.raise_for_status()
        raw = res.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        raw = raw[start_idx:end_idx]
        keep_ids = {item["id"] for item in json.loads(raw)}

        result = []
        for i, a in enumerate(urgent):
            if (i + 1) in keep_ids:
                result.append(a)
            else:
                a["grade"] = "주의"
                a["customer_notice"] = None
                result.append(a)
        return result + others
    except Exception as e:
        print(f"  긴급 강등 오류: {e} — 원본 유지")
        return articles


def ai_filter_and_grade(articles: list) -> list:
    if not articles:
        return []
    result = []
    batch_size = 50
    for i in range(0, len(articles), batch_size):
        batch = articles[i:i+batch_size]
        print(f"  배치 {i//batch_size+1}/{-(-len(articles)//batch_size)} 처리 중... ({len(batch)}건)")
        result.extend(ai_filter_batch(batch, offset=i))
        if i + batch_size < len(articles):
            time.sleep(1)

    if len(result) > 1:
        result = dedup_by_title(result)

    result = regrade_urgent(result)
    return result


def build_exposure_html(entity: str, exposure_data: dict, ref_date: str) -> str:
    rows = find_exposure(entity, exposure_data)
    if not rows:
        return ""
    date_label = f" (기준일: {ref_date})" if ref_date else ""
    items_html = "".join([
        f'<div style="font-size:13px;color:#1e293b;margin-bottom:3px;"><span style="font-weight:bold;">{row.get("종목명","")}</span> ({row.get("종목유형","")}) : {float(str(row.get("잔고(억)","0")).replace(",","")):.1f}억원 / {int(float(str(row.get("고객수","0")).replace(",",""))):,}명</div>'
        for row in rows
    ])
    return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#fff8f8" style="background:#fff8f8;border-left:3px solid #c0392b;">
      <tr><td bgcolor="#fff8f8" style="padding:10px 16px;background:#fff8f8;">
        <p style="margin:0 0 4px 0;font-size:11px;font-weight:bold;color:#c0392b;letter-spacing:0.3px;">뱅키스 고객 보유현황{date_label}</p>
        {items_html}
      </td></tr>
    </table>'''


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = '', exposure_data: dict = None, ref_date: str = '', competitor_notices: list = None, today_str: str = ''):
    exposure_data = exposure_data or {}
    now = datetime.now(timezone(timedelta(hours=9)))
    sections = {"긴급": [], "주의": [], "참고": []}
    for a in articles:
        sections[a["grade"]].append(a)

    GRADE_STYLE = {
        "긴급": {"header_bg":"#fdf0ef","border_left":"#e57373","label_color":"#c0392b","card_bg":"#fff8f8","card_border":"#f5c6c6"},
        "주의": {"header_bg":"#fefce8","border_left":"#f0b429","label_color":"#b7791f","card_bg":"#fffdf0","card_border":"#f5e09a"},
        "참고": {"header_bg":"#f0faf4","border_left":"#48bb78","label_color":"#276749","card_bg":"#f8fff9","card_border":"#b2dfca"},
    }
    rows = ""
    GRADE_LIMIT = {"긴급": 999, "주의": 5, "참고": 999}
    GRADE_DESC = {"긴급": "확정된 손실·부실·제재 — 당일 내 확인·점검 필요", "주의": "손실·부실 가능성 — 주시 및 선제 점검 권고", "참고": "직접 손실 없는 동향 — 참고 파악용"}

    for grade in ["긴급", "주의", "참고"]:
        items = sections[grade]
        if not items:
            continue
        gs = GRADE_STYLE[grade]
        limit = GRADE_LIMIT[grade]
        display_items = items[:limit]
        extra_items = items[limit:]
        rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;border:1px solid {gs["card_border"]};border-bottom:none;background:{gs["header_bg"]};border-left:4px solid {gs["border_left"]};">
          <tr>
            <td style="padding:10px 14px;">
              <span style="font-size:15px;font-weight:bold;color:{gs["label_color"]};">{grade} · {len(items)}건</span>
            </td>
            <td align="right" class="grade-header-right" style="padding:10px 14px;white-space:nowrap;">
              <span style="font-size:11px;color:#94a3b8;">{GRADE_DESC[grade]}</span>
            </td>
          </tr>
        </table>'''

        for a in display_items:
            if grade == "참고":
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left:1px solid {gs["card_border"]};border-right:1px solid {gs["card_border"]};border-bottom:1px solid {gs["card_border"]};background:#fafafa;">
          <tr>
            <td style="padding:7px 16px;font-size:13px;word-break:keep-all;">
              <a href="{a['url']}" style="color:#475569;text-decoration:none;line-height:1.5;">{a['title']}</a>
            </td>
            <td align="right" valign="middle" style="padding:7px 16px 7px 4px;font-size:11px;color:#94a3b8;white-space:nowrap;">{a.get("pub_str","")}</td>
          </tr>
        </table>'''
            else:
                badges = ""
                if a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#3b5491;background:#e8f0fe;padding:2px 7px;margin-right:4px;margin-bottom:6px;">{a["keyword"]}</span>'
                if a.get("entity") and a.get("entity") != a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#64748b;background:#f1f5f9;padding:2px 7px;margin-right:4px;margin-bottom:6px;">{a["entity"]}</span>'

                if grade == "주의":
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:6px;">
          <tr>
            <td style="padding:10px 18px;">
              {f"<p style='margin:0 0 4px 0;'>{badges}</p>" if badges else ""}
              <a href="{a['url']}" style="font-weight:bold;font-size:14px;text-decoration:none;color:#1e3a6e;line-height:1.6;word-break:keep-all;display:block;">{a['title']}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 6px 0;">
                <tr>
                  <td style="font-size:11px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
              {f'<p style="margin:0 0 2px 0;font-size:11px;font-weight:bold;color:{gs["label_color"]};">대응방안</p><p style="margin:0;font-size:13px;color:#1e293b;line-height:1.5;">{a["action"]}</p>' if a.get("action") else ""}
              {build_exposure_html(a.get("entity",""), exposure_data or {}, ref_date)}
            </td>
          </tr>
        </table>'''
                else:
                    exposure_html = build_exposure_html(a.get("entity",""), exposure_data or {}, ref_date)
                    action_row = f'<tr><td bgcolor="#fff8f8" style="padding:10px 18px;border-bottom:1px dashed {gs["card_border"]};background:#fff8f8;"><p style="margin:0 0 3px 0;font-size:11px;font-weight:bold;color:{gs["label_color"]};">대응방안</p><p style="margin:0;font-size:13px;color:#1e293b;line-height:1.6;font-weight:500;">{a["action"]}</p></td></tr>' if a.get("action") else ""
                    exposure_row = f'<tr><td style="padding:10px 18px;border-bottom:1px dashed {gs["card_border"]};">{exposure_html}</td></tr>' if exposure_html else ""
                    notice_text = (a["customer_notice"][:200] + "...") if a.get("customer_notice") and len(a["customer_notice"]) > 200 else a.get("customer_notice","")
                    notice_row = f'<tr><td bgcolor="#eff6ff" style="padding:10px 16px;background:#eff6ff;"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;"><span style="background:#2563eb;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;">✦ AI</span><span style="color:#1d4ed8;">LMS 추천 문구</span></p><p style="margin:0;font-size:12px;color:#1e3a6e;line-height:1.7;white-space:pre-line;">{notice_text}</p></td></tr>' if a.get("customer_notice") else ""
                    bottom_box = f'<tr><td bgcolor="#fff8f8" style="background:#fff8f8;border-top:1px solid {gs["card_border"]};padding:0;"><table width="100%" cellpadding="0" cellspacing="0" border="0">{action_row}{exposure_row}{notice_row}</table></td></tr>' if (action_row or exposure_row or notice_row) else ""
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:10px;">
          <tr>
            <td bgcolor="#fff8f8" style="padding:12px 16px;background:#fff8f8;border-bottom:1px solid #f5c6c6;">
              {f"<p style='margin:0 0 8px 0;'>{badges}</p>" if badges else ""}
              <a href="{a['url']}" style="font-weight:bold;font-size:15px;text-decoration:none;color:#1e3a6e;line-height:1.6;word-break:keep-all;display:block;">{a['title']}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:5px 0 8px 0;">
                <tr>
                  <td style="font-size:12px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
              {f'<p style="margin:0;font-size:12px;color:#64748b;line-height:1.6;">{a["desc"]}</p>' if a.get("desc") else ""}
            </td>
          </tr>
          {bottom_box}
        </table>'''

        if extra_items:
            extra_rows = "".join([f'''
            <tr>
              <td style="padding:4px 0;font-size:13px;color:#475569;border-bottom:1px solid #f0f0f0;">
                <a href="{e['url']}" style="color:#475569;text-decoration:none;">{e['title'][:60]}{"..." if len(e['title']) > 60 else ""}</a>
              </td>
            </tr>''' for e in extra_items])
            rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:#fafafa;margin-bottom:10px;">
          <tr><td style="padding:10px 16px 4px 16px;">
            <p style="margin:0 0 8px 0;font-size:12px;font-weight:bold;color:#64748b;">추가 {len(extra_items)}건</p>
            <table width="100%" cellpadding="0" cellspacing="0" border="0">{extra_rows}</table>
          </td></tr>
        </table>'''

    html = f"""<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  @media only screen and (max-width: 600px) {{
    .outer {{ padding: 8px !important; }}
    .main {{ width: 100% !important; max-width: 100% !important; }}
    .grade-header-right {{ display: none !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f9;">
<tr><td align="center" class="outer" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr>
    <td style="background:#3b5491;padding:22px 26px;">
      <p style="margin:0 0 4px 0;font-size:20px;font-weight:bold;color:#ffffff;">🤖 eBiz본부 리스크 탐지봇
        <span style="font-size:12px;color:#ffffff;padding:2px 8px;background:#5a7abf;margin-left:8px;">Powered by Claude AI</span>
      </p>
      <p style="margin:0 0 14px 0;font-size:13px;color:#c8d8f0;">
        {now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 · 수집 {total_count}건 → {len(articles)}건 선별 ({round((1 - len(articles)/total_count)*100) if total_count else 0}% 필터링)
      </p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#2d4278;">
        <tr>
          <td align="center" style="padding:12px 8px;border-right:1px solid #4a6099;">
            <p style="margin:0 0 2px 0;font-size:22px;font-weight:bold;color:#ff6b6b;">{len(sections['긴급'])}</p>
            <p style="margin:0;font-size:12px;color:#d0dcf0;">긴급</p>
          </td>
          <td align="center" style="padding:12px 8px;border-right:1px solid #4a6099;">
            <p style="margin:0 0 2px 0;font-size:22px;font-weight:bold;color:#fbbf24;">{len(sections['주의'])}</p>
            <p style="margin:0;font-size:12px;color:#d0dcf0;">주의</p>
          </td>
          <td align="center" style="padding:12px 8px;">
            <p style="margin:0 0 2px 0;font-size:22px;font-weight:bold;color:#6ee7b7;">{len(sections['참고'])}</p>
            <p style="margin:0;font-size:12px;color:#d0dcf0;">참고</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  {('<tr><td style="padding:14px 22px;border-bottom:1px solid #e2e8f0;background:#f8fbff;">' + build_summary_html(ai_summary) + '</td></tr>') if ai_summary else ""}
  {('<tr><td>' + build_competitor_html(competitor_notices or [], today_str) + '</td></tr>') if competitor_notices else ""}
  <tr><td style="padding:0 22px 16px 22px;">{rows}</td></tr>
  <tr>
    <td style="padding:14px 22px;background:#fff;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;line-height:2.0;">
        본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
        담당자<br>
        &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(정) 최진후 차장<br>
        &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
      </p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>"""
    return html


def build_empty_html(now) -> str:
    return f"""<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f9;">
<tr><td align="center" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr><td style="background:#3b5491;padding:22px 26px;">
    <p style="margin:0 0 6px 0;font-size:20px;font-weight:bold;color:#ffffff;">🤖 eBiz본부 리스크 탐지봇</p>
    <p style="margin:0;font-size:14px;color:#c8d8f0;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간)</p>
  </td></tr>
  <tr><td align="center" style="padding:40px 24px;">
    <p style="margin:0;font-size:17px;color:#64748b;line-height:1.8;">AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.</p>
  </td></tr>
  <tr><td style="padding:14px 22px;border-top:1px solid #e2e8f0;">
    <p style="margin:0;font-size:12px;color:#94a3b8;line-height:2.0;">
      담당자<br>
      &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(정) 최진후 차장<br>
      &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def send_email_no_result(subject: str, html_body: str):
    receiver = NO_RESULT_RECEIVER if NO_RESULT_RECEIVER else EMAIL_SENDER
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = receiver
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [receiver], msg.as_string())


def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(EMAIL_RECEIVERS)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
    print("이메일 발송 완료")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 뉴스 모니터링 시작")
    print(f"  Volume 경로: {VOLUME_DIR}")
    now_kst   = datetime.now(timezone(timedelta(hours=9)))
    seen_urls = load_seen_urls()
    raw_articles = []

    def crawl_keyword(keyword):
        articles = crawl_naver_news(keyword)
        kst_tz = timezone(timedelta(hours=9))
        result = []
        for article in articles:
            if article["url"]:
                try:
                    pub_dt = _pdt(article.get("pubDate","")).astimezone(kst_tz)
                    elapsed = now_kst - pub_dt
                    hours = int(elapsed.total_seconds() // 3600)
                    mins = int((elapsed.total_seconds() % 3600) // 60)
                    elapsed_str = f"{hours}시간 전" if hours > 0 else f"{mins}분 전"
                    article["pub_str"] = f"{pub_dt.strftime('%m/%d %H:%M')} ({elapsed_str})"
                except Exception:
                    article["pub_str"] = ""
                result.append(article)
        return keyword, result

    print(f"  키워드 {len(KEYWORDS)}개 병렬 크롤링 중...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(crawl_keyword, kw): kw for kw in KEYWORDS}
        for future in as_completed(futures):
            try:
                keyword, articles = future.result()
                new = []
                for article in articles:
                    if article["url"] not in seen_urls:
                        new.append(article)
                        seen_urls.add(article["url"])
                raw_articles.extend(new)
                print(f"  [{keyword}] 신규 {len(new)}건")
            except Exception as e:
                print(f"  크롤링 오류: {e}")

    save_seen_urls(seen_urls)

    if not raw_articles:
        print("신규 뉴스 없음 — 결과 없음 메일 발송")
        now = datetime.now(timezone(timedelta(hours=9)))
        now_str = now.strftime("%m월%d일 %H시")
        subject = f"(eBiz본부) [결과없음] 리스크 탐지_{now_str} 기준 — 신규 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        return

    print(f"\nAI 필터링 중... (총 {len(raw_articles)}건)")
    filtered = ai_filter_and_grade(raw_articles)
    print(f"필터링 후 {len(filtered)}건 선별")

    if not filtered:
        print("AI 필터링 결과 없음 — 결과 없음 메일 발송")
        now = datetime.now(timezone(timedelta(hours=9)))
        now_str = now.strftime("%m월%d일 %H시")
        subject = f"(eBiz본부) [결과없음] 리스크 탐지_{now_str} 기준 — 해당 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        return

    print("  본문 크롤링 중...")
    def crawl_body(article):
        article["body"] = fetch_article_body(article["url"])
        return article

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(crawl_body, a): a for a in filtered}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    print("  대응방안 재생성 중...")
    exposure_data = load_exposure_data()

    def regenerate_action(article):
        if article.get("grade") == "참고":
            return
        body_text = article.get("body", "")
        if not body_text:
            return
        try:
            action_res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": f"""한국투자증권 eBiz본부 리스크 담당자 입장에서 아래 기사 본문을 읽고 즉시 취해야 할 구체적 조치를 작성하세요.
규칙: 보고·공유·전달 등 보고 행위 제외. 실제 확인·점검·산출 등 실무 행동만. 한 문장, 50자 이내.
등급: {article['grade']} / 제목: {article['title']} / 본문: {body_text[:400]}
{f"익스포저: {', '.join([r.get('종목유형','') + ' ' + r.get('잔고(억)','') + '억원' for r in find_exposure(article.get('entity',''), exposure_data)])}" if find_exposure(article.get('entity',''), exposure_data) else ""}
조치만 한 문장으로 반환하세요."""}],
                },
                timeout=10,
            )
            new_action = action_res.json()["content"][0]["text"].strip()
            if new_action:
                article["action"] = new_action
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(regenerate_action, a) for a in filtered]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    print("  고객 안내 문구 생성 중...")

    def generate_customer_notice(article):
        if article.get("grade") != "긴급":
            return
        body_text = article.get("body", "")
        entity = article.get("entity", "")
        keyword = article.get("keyword", "")
        try:
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": f"""한국투자증권 eBiz본부 비대면 채널 담당자입니다.
아래 기사를 바탕으로 LMS 발송용 고객 안내 문구를 작성하세요.
5줄 이내, 문구만 반환, 번호나 설명 없이.
키워드: {keyword} / 기업명: {entity} / 제목: {article['title']} / 본문: {body_text[:400]}"""}],
                },
                timeout=10,
            )
            notice = res.json()["content"][0]["text"].strip()
            if notice:
                article["customer_notice"] = notice
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(generate_customer_notice, a) for a in filtered]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    now = datetime.now(timezone(timedelta(hours=9)))
    today_str = now.strftime("%m월 %d일")
    competitor_notices = load_competitor_notices()

    if exposure_data:
        ref_date = next(iter(exposure_data.values())).get("기준일", "")
    else:
        ref_date = ""

    now_str = now.strftime("%m월%d일 %H시")
    urgent_count = len([a for a in filtered if a.get("grade") == "긴급"])
    subject = f"(eBiz본부) 리스크 탐지{'_긴급'+str(urgent_count)+'건' if urgent_count > 0 else ''}_{now_str} 기준"
    total_count = len(raw_articles)

    # AI 전체 요약
    urgent_cnt = len([a for a in filtered if a["grade"]=="긴급"])
    caution_cnt = len([a for a in filtered if a["grade"]=="주의"])
    ref_cnt = len([a for a in filtered if a["grade"]=="참고"])
    filtered_titles = f"[등급 분포] 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건\n\n" + "\n".join([f"- [{a['grade']}] {a['title']}" for a in filtered])
    ai_summary = ""
    try:
        sum_res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": f"아래 오늘의 리스크 기사 목록을 보고, 증권사 리스크 담당자를 위해 아래 형식으로 작성하세요.\n\n▸ 리스크 성격\n(오늘 전반적인 리스크 흐름을 30자 이내 한 문장)\n\n▸ 주요 포인트\n(담당자가 주목할 핵심 사항을 · 로 구분, 항목당 30자 이내, 최대 3개)\n\n반드시 짧고 핵심만.\n\n{filtered_titles}"}],
            },
            timeout=15,
        )
        ai_summary = sum_res.json()["content"][0]["text"].strip()
    except Exception:
        pass

    html = build_email_html(filtered, total_count=total_count, ai_summary=ai_summary, exposure_data=exposure_data, ref_date=ref_date, competitor_notices=competitor_notices, today_str=today_str)
    send_email(subject, html)


if __name__ == "__main__":
    main()

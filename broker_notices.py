import os
import re
import json
import traceback
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime

# ─────────────────────────────────────────────
# Railway Volume 경로 설정
# ─────────────────────────────────────────────
VOLUME_DIR = os.environ.get("VOLUME_DIR", "/data")
DATA_DIR   = os.path.join(VOLUME_DIR, "data")
DEBUG_DIR  = os.path.join(VOLUME_DIR, "debug")
MERGED_CSV = os.path.join(DATA_DIR, "broker_notices_merged.csv")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

print(f"[broker_notices] Volume 경로: {VOLUME_DIR}")


COMPANIES = {
    "SAMSUNG": "삼성증권",
    "KIWOOM": "키움증권",
    "NH": "NH투자증권",
    "KB": "KB증권",
    "MIRAE": "미래에셋증권",
    "SHINHAN": "신한투자증권",
    "MERITZ": "메리츠증권",
}


def get_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    return session


def clean_text(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def normalize_date(value):
    value = clean_text(value)
    if not value:
        return ""
    value = value.replace(".", "-").replace("/", "-")
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return value


def classify_notice(title):
    title = clean_text(title)
    rules = [
        ("규제", ["규제", "투자자 보호", "제도", "유의", "법령", "시행"]),
        ("상품", ["신용", "대출", "CFD", "증거금", "발행어음", "RP", "채권", "ETF", "ETN"]),
        ("마케팅", ["이벤트", "수수료", "혜택", "쿠폰", "무료", "리워드"]),
        ("서비스", ["서비스", "약관", "변경", "개정", "오픈", "종료", "인증"]),
        ("실적", ["실적", "매출", "손익"]),
        ("시스템", ["점검", "중단", "장애", "오류", "전산", "오픈뱅킹", "진위확인"]),
    ]
    for category, keywords in rules:
        if any(k in title for k in keywords):
            return category
    return "없음"


def make_row(company, date, title, notice_id="", url="", views="", source_type="", raw=None):
    title = clean_text(title)
    date = normalize_date(date)
    return {
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "company": company,
        "date": date,
        "title": title,
        "notice_id": str(notice_id) if notice_id is not None else "",
        "category": classify_notice(title),
        "views": views,
        "url": url,
        "source_type": source_type,
        "raw": json.dumps(raw, ensure_ascii=False) if raw is not None else "",
    }


def make_dedup_key(df):
    return df.apply(
        lambda r: (
            f"{r['company']}|id|{r['notice_id']}"
            if clean_text(r.get("notice_id")) else
            f"{r['company']}|title|{r['date']}|{r['title']}"
        ),
        axis=1
    )


def save_csv_dedup(rows, company_name=None):
    if not rows:
        return pd.DataFrame(), pd.DataFrame()
    new_df = pd.DataFrame(rows).fillna("")
    if company_name:
        company_csv = os.path.join(DATA_DIR, f"{company_name}.csv")
        saved_df, new_only_df = save_one_csv(new_df, company_csv)
        return saved_df, new_only_df
    return pd.DataFrame(), pd.DataFrame()


def save_one_csv(new_df, path):
    new_df = new_df.fillna("")
    if os.path.exists(path):
        old_df = pd.read_csv(path, dtype=str).fillna("")
    else:
        old_df = pd.DataFrame(columns=new_df.columns)
    old_keys = set(make_dedup_key(old_df)) if len(old_df) else set()
    combined = pd.concat([old_df, new_df], ignore_index=True).fillna("")
    combined["dedup_key"] = make_dedup_key(combined)
    new_df["dedup_key"] = make_dedup_key(new_df)
    new_only_df = new_df[~new_df["dedup_key"].isin(old_keys)].drop(columns=["dedup_key"])
    combined = combined.drop_duplicates(subset=["dedup_key"], keep="last")
    combined = combined.drop(columns=["dedup_key"])
    combined = combined.sort_values(["date", "company"], ascending=[False, True])
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    return combined, new_only_df


def save_merged_csv():
    csv_files = [
        os.path.join(DATA_DIR, f)
        for f in os.listdir(DATA_DIR)
        if f.endswith(".csv") and f != "broker_notices_merged.csv"
    ]
    if not csv_files:
        return pd.DataFrame()
    dfs = []
    for path in csv_files:
        try:
            dfs.append(pd.read_csv(path, dtype=str).fillna(""))
        except Exception:
            pass
    if not dfs:
        return pd.DataFrame()
    merged = pd.concat(dfs, ignore_index=True).fillna("")
    merged["dedup_key"] = make_dedup_key(merged)
    merged = merged.drop_duplicates(subset=["dedup_key"], keep="last")
    merged = merged.drop(columns=["dedup_key"])
    merged = merged.sort_values(["date", "company"], ascending=[False, True])
    merged.to_csv(MERGED_CSV, index=False, encoding="utf-8-sig")
    return merged


def safe_json(res, crawler_name):
    try:
        return res.json()
    except Exception:
        debug_path = os.path.join(DEBUG_DIR, f"{crawler_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(res.text)
        print(f"[DEBUG 저장] {debug_path}")
        raise


def crawl_meritz(page=1, list_cnt=10):
    company = COMPANIES["MERITZ"]
    session = get_session()
    base_page = "https://home.imeritz.com/mobile/content/support/notice_list.html"
    api_url = "https://home.imeritz.com/bbs/newBbsList.do"
    headers = {
        "Referer": base_page,
        "Origin": "https://home.imeritz.com",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    session.get(base_page, headers=headers, timeout=15)
    payload = {
        "bbsGrpId": "bascGrp",
        "bbsId": "help13v",
        "listCnt": str(list_cnt),
        "pageNum": str(page),
        "viewUrl": "notice_view.html",
        "searchDiv": "00",
        "searchText": "",
    }
    res = session.post(api_url, headers=headers, data=payload, timeout=15)
    res.raise_for_status()
    data = safe_json(res, "meritz")
    rows = []
    for item in data.get("bbsList", []):
        notice_id = item.get("bbsCnttTurnNo")
        rows.append(make_row(
            company=company,
            date=item.get("bbsCnttWrtnYmdh"),
            title=item.get("bbsCnttTitlName"),
            notice_id=notice_id,
            url=f"{base_page}#notice_id={notice_id}",
            views=item.get("bbsCnttIqryCnt"),
            source_type="json_api",
            raw=item,
        ))
    return rows


def crawl_mirae(page=1):
    company = COMPANIES["MIRAE"]
    session = get_session()
    url = f"https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=1490&curPage={page}"
    res = session.get(url, timeout=15)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    rows = []
    for a in soup.select("a[href*='viewDetail']"):
        title = clean_text(a.get_text(" ", strip=True))
        href = a.get("href", "")
        m = re.search(r"viewDetail\('([^']+)'\s*,\s*'([^']+)'\)", href)
        notice_id = m.group(1) if m else ""
        parent_text = a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
        dm = re.search(r"\d{4}[./-]\d{2}[./-]\d{2}", parent_text)
        date = dm.group(0) if dm else ""
        if title:
            rows.append(make_row(
                company=company,
                date=date,
                title=title,
                notice_id=notice_id,
                url=url,
                source_type="html",
                raw={"href": href},
            ))
    return rows


def crawl_nh(page=1):
    url = "https://m.nhsec.com/customer/notice/noticeList.json"
    payload = {
        "page": str(page),
        "searchType": "whole",
        "keyWord": "",
        "t": str(int(datetime.now().timestamp() * 1000)),
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Origin": "https://m.nhsec.com",
        "Referer": "https://m.nhsec.com/customer/notice/noticeList",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    res = requests.post(url, headers=headers, data=payload, timeout=15)
    res.raise_for_status()
    data = safe_json(res, "nh")
    items = data.get("resultList", {}).get("content", [])
    rows = []
    for item in items:
        rows.append(make_row(
            company="NH투자증권",
            date=item.get("mInputDttm") or item.get("mInputDtm"),
            title=item.get("mTitle"),
            notice_id=item.get("mNo"),
            views=item.get("mReadCnt"),
            url="https://m.nhsec.com/customer/notice/noticeList",
            source_type="json_api",
            raw=item,
        ))
    return rows


def crawl_shinhan(page=1):
    company = COMPANIES["SHINHAN"]
    session = get_session()
    page_url = "https://m.shinhansec.com/"
    api_url = "https://bbs2.shinhansec.com/mobile/json.list.do"
    headers = {
        "Referer": page_url,
        "Origin": "https://m.shinhansec.com",
    }
    params = {
        "boardName": "nNoticeCFM",
        "curPage": str(page),
    }
    session.get(page_url, headers=headers, timeout=15)
    res = session.get(api_url, headers=headers, params=params, timeout=15)
    res.raise_for_status()
    data = safe_json(res, "shinhan")
    items = (
        data.get("listPage", {}).get("list")
        or data.get("list")
        or data.get("resultList")
        or data.get("data")
        or []
    )
    rows = []
    for item in items:
        title = item.get("f1") or item.get("title") or item.get("subject")
        date = item.get("f0") or item.get("regDate") or item.get("date")
        notice_id = item.get("f4") or item.get("seq") or item.get("id")
        views = item.get("f3") or item.get("views")
        rows.append(make_row(
            company=company,
            date=date,
            title=title,
            notice_id=notice_id,
            views=views,
            url=f"{page_url}#boardName=nNoticeCFM",
            source_type="json_api",
            raw=item,
        ))
    return rows


def crawl_samsung(page=1, rows_per_page=10):
    company = COMPANIES["SAMSUNG"]
    session = get_session()
    page_url = "https://www.samsungpop.com/ux/kor/customer/notice/notice/noticeList.do"
    api_url = "https://www.samsungpop.com/ux/kor/customer/notice/notice/getNoticeList.do"
    headers = {
        "Referer": page_url,
        "Origin": "https://www.samsungpop.com",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    session.get(page_url, headers=headers, timeout=15)
    payload = {
        "currentPage": str(page),
        "rowsPerPage": str(rows_per_page),
        "Search": "1",
        "SearchText": "",
        "searchStartDate": "",
        "searchEndDate": "",
        "tabIndex": "0",
        "searchType": "0",
        "siteGubun": "KF",
        "af_auto_index": "start",
        "ajaxQuery": "1",
    }
    res = session.post(api_url, headers=headers, data=payload, timeout=15)
    res.raise_for_status()
    data = safe_json(res, "samsung")
    items = (
        data.get("list")
        or data.get("result")
        or data.get("noticeList")
        or data.get("data")
        or []
    )
    rows = []
    for item in items:
        title = item.get("ntcTitle1") or item.get("ntcTitle") or item.get("title")
        date = item.get("procDTime2") or item.get("procDtime") or item.get("regDate")
        notice_id = item.get("menuSeqNo") or item.get("NO") or item.get("ntcNo")
        views = item.get("ntcObjRefCnt") or item.get("inqCnt") or item.get("views")
        rows.append(make_row(
            company=company,
            date=date,
            title=title,
            notice_id=notice_id,
            views=views,
            url=page_url,
            source_type="json_api",
            raw=item,
        ))
    return rows


def crawl_kb(page=1):
    company = COMPANIES["KB"]
    session = get_session()
    page_url = "https://www.kbsec.com/go.able?linkcd=m06090001"
    api_url = "https://www.kbsec.com/go.able?linkcd=CJTRAJAX2"
    headers = {
        "Referer": page_url,
        "Origin": "https://www.kbsec.com",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "text/plain, */*; q=0.01",
    }
    session.get(page_url, headers=headers, timeout=15)
    payload = {
        "sSimPageName": "",
        "sSimScrName": "[공지사항목록조회]",
        "sSimTrInput": "404,H,A,0,,,0,Y,,,,",
        "sSimTrcd": "VIS80100",
        "sSimNextGubun": "",
        "sSimOp": "1",
        "isLoadImgManual": "",
    }
    res = session.post(api_url, headers=headers, data=payload, timeout=15)
    res.raise_for_status()
    data = safe_json(res, "kb")
    grid = data.get("grid") or data.get("Grid") or []
    rows = []
    for row in grid:
        title = row[5] if len(row) > 5 else ""
        if not clean_text(title):
            continue
        rows.append(make_row(
            company=company,
            date=row[0] if len(row) > 0 else "",
            title=title,
            notice_id=row[3] if len(row) > 3 else "",
            views=row[13] if len(row) > 13 else "",
            url=page_url,
            source_type="grid_json",
            raw=row,
        ))
    return rows


def crawl_kiwoom(page=1):
    company = COMPANIES["KIWOOM"]
    session = get_session()
    page_url = "https://bbn.kiwoom.com/bbs/VBbsNoticeNWWNZView"
    api_url = "https://bbn.kiwoom.com/bbs/SBbsNoticeNWWNZListAjax"
    headers = {
        "Referer": page_url,
        "Origin": "https://bbn.kiwoom.com",
        "X-Requested-With": "XMLHttpRequest",
        "X-Reply-With": "ajax",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*, application/evfwTetcJ;q=0.1",
    }
    first = session.get(page_url, headers=headers, timeout=15)
    print("KIWOOM PAGE STATUS:", first.status_code)
    payload = {
        "pageNo": str(page),
        "divTp1": "",
        "f_keyField": "ttl",
        "f_key": "",
        "cd": "0",
        "sdate": "",
        "edate": "",
        "_reqAgent": "ajax",
    }
    res = session.post(api_url, headers=headers, data=payload, timeout=15)
    if res.status_code != 200:
        return []
    data = safe_json(res, "kiwoom")
    items = (
        data if isinstance(data, list)
        else data.get("noticeList")
        or data.get("list")
        or data.get("data")
        or data.get("resultList")
        or data.get("rows")
        or []
    )
    rows = []
    for item in items:
        rows.append(make_row(
            company="키움증권",
            date=item.get("makeTime"),
            title=item.get("ttl"),
            notice_id=item.get("seqid"),
            views=item.get("readCnt"),
            url="https://bbn.kiwoom.com/bbs/VBbsNoticeNWWNZView",
            source_type="json_api",
            raw=item,
        ))
    return rows


def run_all():
    crawlers = [
        ("meritz", crawl_meritz),
        ("mirae", crawl_mirae),
        ("nh", crawl_nh),
        ("shinhan", crawl_shinhan),
        ("samsung", crawl_samsung),
        ("kb", crawl_kb),
        ("kiwoom", crawl_kiwoom),
    ]
    total_new = []
    for key, crawler in crawlers:
        try:
            print(f"\n수집 시작: {crawler.__name__}")
            rows = crawler(page=1)
            print(f"수집 완료: {crawler.__name__} / {len(rows)}건")
            if rows:
                saved_df, new_only_df = save_csv_dedup(rows, key)
                print(f"저장 완료: {DATA_DIR}/{key}.csv / 총 {len(saved_df)}건 / 신규 {len(new_only_df)}건")
                total_new.append(new_only_df)
        except Exception as e:
            print(f"[ERROR] 수집 실패: {crawler.__name__}")
            print(type(e).__name__, e)
            traceback.print_exc()

    merged = save_merged_csv()
    print(f"\n통합 저장 완료: {MERGED_CSV} / 총 {len(merged)}건")
    return merged


if __name__ == "__main__":
    df = run_all()
    print(df.head())

"""GitHub 대기 자료와 구독 분석 결과 연결. 모델 API 호출 없이 기존 판정·메일 재사용."""
import argparse
import base64
import copy
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
SCHEMA = 1
POLICY_VERSION = "insight-subscription-v1"
CONFIG = Path("subscription_config.json")
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
THREATS = {"신규진출", "기능강화", "수수료경쟁", "제휴·지분", "플랫폼확장"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()


def seen_hash():
    p = Path("seen_articles.json")
    if not p.exists():
        raise RuntimeError("기존 처리 이력 없음 — 빈 이력으로 실행하지 않음")
    json.loads(p.read_text(encoding="utf-8"))
    return hashlib.sha256(p.read_bytes()).hexdigest()


def article_id(article):
    return digest([article.get("link"), article.get("title"), article.get("pubDate")])[:24]


def now():
    return datetime.now(UTC)


class Queue:
    def __init__(self, config):
        self.repo, self.branch, self.prefix = (config["queue_repository"],
                                              config["queue_branch"], config["queue_prefix"])
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo):
            raise RuntimeError("대기 저장소 미설정")
        self.token = os.environ.get("INSIGHT_QUEUE_TOKEN", "")
        if not self.token:
            raise RuntimeError("INSIGHT_QUEUE_TOKEN 미등록 — 비공개 저장소 연결 불가")

    def request(self, suffix, method="GET", payload=None):
        return self.api_request("/repos/" + self.repo + suffix, method, payload)

    def api_request(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request("https://api.github.com" + path,
                                     data=data, method=method, headers={
                                         "Authorization": "Bearer " + self.token,
                                         "Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28",
                                         "Content-Type": "application/json"})
        # Retry only reads. A lost PUT/POST response may already have committed.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code == 404 and method == "GET":
                    return None
                if e.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(attempt + 1)
                    continue
                raise RuntimeError(f"비공개 대기 저장소 {method} 실패 HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt + 1 < attempts:
                    time.sleep(attempt + 1)
                    continue
                raise RuntimeError(f"비공개 대기 저장소 {method} 네트워크 실패") from None

    def initialize(self):
        """소유자가 요청한 새 비공개 큐를 등록된 PAT로 초기화한다."""
        owner, name = self.repo.split("/")
        user = self.api_request("/user")
        if not user or user.get("login") != owner:
            raise RuntimeError("큐 초기화 키의 소유자 불일치")
        meta = self.request("")
        if meta is None:
            meta = self.api_request("/user/repos", "POST", {
                "name": name, "private": True, "auto_init": True,
                "description": "Private insight subscription input, analysis and delivery ledger"})
        if not meta or not meta.get("private"):
            raise RuntimeError("기존 공개 저장소를 큐로 사용하지 않음")
        if not self.request("/git/ref/heads/" + urllib.parse.quote(self.branch, safe="")):
            ref = self.request("/git/ref/heads/" + urllib.parse.quote(meta["default_branch"], safe=""))
            if not ref:
                raise RuntimeError("큐 초기 커밋 없음")
            self.request("/git/refs", "POST", {"ref": "refs/heads/" + self.branch,
                                              "sha": ref["object"]["sha"]})
        print("비공개 인사이트 대기 저장소 초기화 확인; API0/SMTP0")

    def preflight(self):
        meta = self.request("")
        if not meta or not meta.get("private"):
            raise RuntimeError("대기 저장소 접근 실패 또는 비공개가 아님")
        ref = self.request("/git/ref/heads/" + urllib.parse.quote(self.branch, safe=""))
        if not ref:
            raise RuntimeError("전용 대기 브랜치 없음")
        return True

    def get(self, path):
        suffix = "/contents/" + self.prefix + "/" + path
        obj = self.request(suffix + "?ref=" + urllib.parse.quote(self.branch, safe=""))
        if obj is None:
            return None, None
        if obj.get("type") != "file":
            raise RuntimeError("대기 자료 형식 오류")
        return json.loads(base64.b64decode(obj["content"])), obj["sha"]

    def put(self, path, value, sha=None):
        payload = {"branch": self.branch,
                   "message": "chore: insight subscription state [skip ci]",
                   "content": base64.b64encode(json.dumps(value, ensure_ascii=False,
                                                          indent=2).encode()).decode()}
        if sha:
            payload["sha"] = sha
        return self.request("/contents/" + self.prefix + "/" + path, "PUT", payload)


def validate(job, result, current_seen_hash, at=None):
    at = at or now()
    if job.get("schema_version") != SCHEMA or result.get("schema_version") != SCHEMA:
        raise ValueError("스키마 버전 불일치")
    if result.get("job_id") != job.get("job_id") or result.get("input_hash") != digest(job["input"]):
        raise ValueError("다른 회차·변경된 입력의 결과")
    if job.get("input_hash") != digest(job["input"]) or job["input"].get("policy_version") != POLICY_VERSION:
        raise ValueError("입력 해시·분석 정책 버전 오류")
    if datetime.fromisoformat(job["expires_at"]) < at:
        raise ValueError("분석 결과 만료 — 자동 발송 보류")
    if job["input"]["seen_hash"] != current_seen_hash:
        raise ValueError("처리 이력 변경 — 중복 가능성으로 보류")
    if result.get("analysis_engine") != "chatgpt_subscription":
        raise ValueError("구독 분석 엔진 증거 없음")
    items = job["input"]["items"]
    by_id = {a["article_id"]: a for a in items}
    if len(by_id) != len(items):
        raise ValueError("입력 자체에 중복 ID")
    raw_by_id = {a["article_id"]: a for a in job["input"]["raw_articles"]}
    if len(raw_by_id) != len(job["input"]["raw_articles"]) or any(raw_by_id.get(k) != v for k, v in by_id.items()):
        raise ValueError("수집 자료·선별 자료 불일치")
    rows = result.get("classifications")
    if not isinstance(rows, list) or len(rows) != len(by_id):
        raise ValueError("입력 처리 건수 누락")
    ids = [r.get("article_id") for r in rows if isinstance(r, dict)]
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ValueError("입력 ID 누락·중복·추가")
    for row in rows:
        if type(row.get("relevant")) is not bool or not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("판정 또는 근거 미완성")
    wanted = {r["article_id"] for r in rows if r["relevant"]}
    selected = [a for a in items if a["article_id"] in wanted]
    selected.sort(key=lambda a: (a.get("_tier", "") != "1군", a.get("_source_type", "") != "targeted"))
    selected = selected[:15]
    analyses = result.get("analyses")
    if not isinstance(analyses, dict) or set(analyses) != {a["article_id"] for a in selected}:
        raise ValueError("심층 분석 상한·선정 ID 불일치")
    for article in selected:
        a = analyses[article["article_id"]]
        if not isinstance(a, dict):
            raise ValueError("심층 분석 미완성")
        score, level = a.get("impact_score"), a.get("impact_level")
        if type(score) not in {int, float} or not math.isfinite(score) or not 1 <= score <= 10:
            raise ValueError("영향도 점수 범위 오류")
        if level != ("상" if score >= 7 else "중" if score >= 5 else "하"):
            raise ValueError("등급·점수 불일치")
        for field, bound in {"company_name": 40, "event_key": 40, "impact_domain": 20,
                             "summary": 90, "action_point": 80}.items():
            if not isinstance(a.get(field), str) or not a[field].strip() or len(a[field]) > bound:
                raise ValueError("분석 필수 필드·길이 오류: " + field)
        if a.get("threat_type") not in THREATS or not isinstance(a.get("impact_tags"), dict):
            raise ValueError("위협 유형·태그 오류")
        for key, bound in {"위협유형": 6, "고객영향": 8}.items():
            tag = a["impact_tags"].get(key)
            if not isinstance(tag, str) or not tag.strip() or len(tag) > bound:
                raise ValueError("영향 태그 미완성")
        if article.get("_body_failed"):
            if score > 4.9 or level != "하" or "[본문 미수집]" not in a["summary"]:
                raise ValueError("본문 미수집 점수·등급·표시 오류")
            # 실패한 페이지에 남은 메뉴·스크립트나 유사 기사 본문은 근거로 쓰지 않는다.
            source = article.get("description", "") or ""
        else:
            source = " ".join([article.get("title", ""), article.get("description", ""), article.get("_body", "")])
        quote = a.get("evidence_quote", "")
        if not isinstance(quote, str) or len(quote) < 8 or quote not in source:
            raise ValueError("원문 근거 인용 없음")
        if score >= 5 and a.get("reviewed") is not True:
            raise ValueError("중·상 등급 근거 재검토 미완료")
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", source.replace(",", "")))
        summary_numbers = set(re.findall(r"\d+(?:\.\d+)?", a["summary"].replace(",", "")))
        if not summary_numbers <= source_numbers:
            raise ValueError("요약에 원문 미확인 수치")
    return {r["article_id"]: r for r in rows}, analyses


class Provider:
    def __init__(self, job, result, bot, current_seen_hash, at=None):
        self.rows, self.analyses = validate(job, result, current_seen_hash, at)
        self.job, self.bot = job, bot

    def collect(self, seen):
        return [copy.deepcopy(a) for a in self.job["input"]["raw_articles"]
                if not self.bot.is_duplicate(a, seen)]

    def relevant(self, articles):
        return [a for a in articles if self.rows[a["article_id"]]["relevant"]]

    def analysis_for(self, article):
        return copy.deepcopy(self.analyses[article["article_id"]])


class DeliveryGuard:
    def __init__(self, queue, job_id):
        self.queue, self.path = queue, "jobs/" + job_id + ".json"
        self.job_id = job_id

    def save_log(self, value):
        self.queue.put("filter-logs/" + self.job_id + ".json", value)

    def change(self, fn):
        job, sha = self.queue.get(self.path)
        if not job or not sha:
            raise RuntimeError("발송 선기록 읽기 실패")
        fn(job)
        self.queue.put(self.path, job, sha)
        return job

    def before(self, address):
        key = hashlib.sha256(address.encode()).hexdigest()
        skip = False
        def reserve(job):
            nonlocal skip
            old = job.setdefault("receipts", {}).get(key)
            if old == "accepted":
                skip = True
                return
            if old in {"sending", "uncertain"}:
                raise RuntimeError("이전 전송 상태 미확인 — 재전송 금지")
            job["receipts"][key] = "sending"
        self.change(reserve)
        return not skip

    def after(self, address, status):
        key = hashlib.sha256(address.encode()).hexdigest()
        self.change(lambda job: job.setdefault("receipts", {}).__setitem__(key, status))

    def require_complete(self):
        job, _ = self.queue.get(self.path)
        if not job or (not job.get("receipts") and not job.get("no_delivery_required")) or any(s != "accepted" for s in job.get("receipts", {}).values()):
            raise RuntimeError("미접수·미확인 수신자 존재 — 처리 이력 저장 보류")


def import_bot():
    os.environ["INSIGHT_ENGINE"] = "subscription"
    import monitor_v2
    monitor_v2.INSIGHT_ENGINE = "subscription"
    monitor_v2.client = None
    return monitor_v2


def notify_error(error, context=""):
    """기존 담당자 경로에 알림을 시도한다. 운영 영수증과 공개 로그는 격리한다."""
    if not os.environ.get("GMAIL_USER") or not os.environ.get("GMAIL_APP_PASSWORD"):
        print("담당자 오류 알림 생략 — SMTP 설정 없음; Actions 실패 상태 확인")
        return
    try:
        bot = import_bot()
        details = context or traceback.format_exc()
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if run_id.isdigit():
            details += "\nActions: https://github.com/jinhoo-choi/competitor_monitor/actions/runs/" + run_id
        guard = bot.DELIVERY_GUARD
        try:
            bot.DELIVERY_GUARD = None
            # 기존 오류 메일 함수의 주소·SMTP 오류 출력도 공개 로그에 남기지 않는다.
            with redirect_stdout(StringIO()):
                accepted = bot.send_email_error("인사이트 구독 경로 보류: " + str(error), details)
        finally:
            bot.DELIVERY_GUARD = guard
        print("담당자 오류 알림 SMTP 접수; 운영 영수증 변경0/API0" if accepted
              else "담당자 오류 알림 접수 미확인; Actions 실패 상태 확인")
    except Exception as notification_error:
        print("담당자 오류 알림 실패:", type(notification_error).__name__)


def collect(config, queue, mode="production"):
    bot = import_bot()
    state_hash = seen_hash()
    queue.preflight()
    job_id = str(os.environ.get("GITHUB_RUN_ID") or now().strftime("%Y%m%dT%H%M%S"))
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("회차 ID 오류")
    index, index_sha = queue.get("index.json")
    index = index or {"jobs": []}
    for old_id in index["jobs"]:
        old, old_sha = queue.get("jobs/" + old_id + ".json")
        if (old and old.get("schema_version") == SCHEMA
                and old.get("status") == "pending" and old.get("receipts") == {}
                and not old.get("delivered_at") and not old.get("seen_after_hash")
                and datetime.fromisoformat(old["expires_at"]) <= now()):
            # SMTP 예약 기록이 전혀 없는 회차만 CAS로 종결한다. 입력·결과·seen은 보존.
            old.update(status="expired", expired_at=now().isoformat())
            queue.put("jobs/" + old_id + ".json", old, old_sha)
            print("미발송 구독 회차 자동 만료:", old_id)
            notify_error(RuntimeError("미발송 구독 회차 만료: " + old_id),
                         "입력과 처리 이력은 보존했습니다. 다음 회차는 17시간 범위만 수집하므로 "
                         "만료 입력 전체의 재수집은 보장되지 않습니다. 비공개 회차 기록을 확인하세요.")
            continue
        if old and old.get("status") in {"pending", "sending", "uncertain", "delivery_complete"}:
            raise RuntimeError("미종결 구독 회차 존재 — 신규 회차·롤백 중복 방지")
    if queue.get("jobs/" + job_id + ".json")[0] is not None:
        raise RuntimeError("동일 회차 재수집 금지")
    raw = bot.collect_articles(bot.load_seen())
    passed, _ = bot.hard_filter(raw)
    def body(article):
        article["article_id"] = article_id(article)
        url = article.get("originallink") or article.get("link")
        text = bot.fetch_article_body(url) if url else ""
        article["_body"] = (text or article.get("description", ""))[:800]
        article["_body_failed"] = not bool(text)
        return article
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(body, passed))
    for article in raw:
        article["article_id"] = article_id(article)
    created = now()
    job = {"schema_version": SCHEMA, "job_id": job_id, "status": "pending", "mode": mode,
           "created_at": created.isoformat(),
           "expires_at": (created + timedelta(minutes=config["job_ttl_minutes"])).isoformat(),
           "input": {"policy_version": POLICY_VERSION, "seen_hash": state_hash,
                     "raw_articles": raw, "items": passed}, "receipts": {}}
    job["input_hash"] = digest(job["input"])
    queue.put("jobs/" + job_id + ".json", job)
    index["jobs"] = (index["jobs"] + [job_id])[-60:]
    queue.put("index.json", index, index_sha)
    print(json.dumps({"job_id": job_id, "status": "pending", "candidates": len(passed),
                      "paid_ai_calls": 0}))


def consume(config, queue, job_id, dry_run=False, self_test=False):
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("회차 ID 오류")
    if dry_run and self_test:
        raise ValueError("렌더링 검수·본인 발송 중 하나만 선택")
    if not dry_run and not self_test and config["engine"] != "subscription":
        raise RuntimeError("운영 전환 미완료 — 구독 결과 실발송 금지")
    bot = import_bot()
    if self_test:
        if not bot.TEST_MODE or bot.RECIPIENTS_ALL or bot.RECIPIENTS_CC or bot.NO_RESULT_RECEIVER != bot.GMAIL_USER:
            raise RuntimeError("본인 발송 검수 격리 설정 실패")
    elif not dry_run and (bot.TEST_MODE or bot.DRY_RUN):
        raise RuntimeError("운영 회차에 검수 환경 설정 금지")
    queue.preflight()
    path = "jobs/" + job_id + ".json"
    job, sha = queue.get(path)
    result, _ = queue.get("results/" + job_id + ".json")
    if not job or result is None:
        raise RuntimeError("대기 자료·분석 결과 미확인")
    if job["status"] in {"completed", "verified"}:
        print("이미 종결된 회차 — 재발송 0")
        return
    if job["status"] != "pending":
        raise RuntimeError("미종결 전송 상태 존재 — 재발송·롤백 보류")
    if (dry_run or self_test) and job.get("mode") != "verification":
        raise RuntimeError("운영 회차를 검수로 종결할 수 없음")
    provider = Provider(job, result, bot, seen_hash())
    # 규칙 정규화도 메일 직전에 몰아서 하지 않고 전부 먼저 확인한다.
    normalized = []
    for article in job["input"]["items"]:
        if article["article_id"] in provider.analyses:
            normalized.append(bot.analyze_article(copy.deepcopy(article), provider.analysis_for(article)))
    if dry_run:
        html = bot.build_email_html([a for a in normalized if a.get("analysis")],
                                    len(job["input"]["raw_articles"]), len(normalized))
        queue.put("previews/" + job_id + ".json", {"html": html, "paid_ai_calls": 0})
        job.update(status="verified", verified_at=now().isoformat(), paid_ai_calls=0)
        queue.put(path, job, sha)
        print("구독 연결·형식·규칙·메일 렌더링 검수 완료; 발송0/API0")
        return
    if not self_test and job.get("mode") != "production":
        raise RuntimeError("검수 자료는 운영 발송에 사용할 수 없음")
    job.update(status="sending", sending_at=now().isoformat())
    queue.put(path, job, sha)
    bot.DELIVERY_GUARD = DeliveryGuard(queue, job_id)
    try:
        # 주소·기사별 판정은 비공개 자료로 보관하고 공개 Actions 로그에는 남기지 않는다.
        output = StringIO()
        with redirect_stdout(output):
            bot.main(provider)
        queue.put("logs/" + job_id + ".json", {"log": output.getvalue(), "paid_ai_calls": 0})
        bot.DELIVERY_GUARD.require_complete()
        if self_test and seen_hash() != job["input"]["seen_hash"]:
            raise RuntimeError("검수 중 처리 이력 변경")
        bot.DELIVERY_GUARD.change(lambda j: j.update(status="verified" if self_test else "delivery_complete",
                                                    paid_ai_calls=0, delivered_at=now().isoformat(),
                                                    self_test=self_test, seen_after_hash=seen_hash(),
                                                    seen_after_snapshot=Path("seen_articles.json").read_text(encoding="utf-8")))
        print("본인 발송 검수 완료; API0/처리 이력 변경0" if self_test else "구독 결과 발송 접수 완료; API0/처리 이력 커밋 대기")
    except Exception:
        # 영수증 선기록 전 실패는 재검수 가능하다. 한 건이라도 예약되면 계속 보호한다.
        bot.DELIVERY_GUARD.change(lambda j: j.update(
            status="pending" if j.get("receipts") == {} and not j.get("delivered_at")
            and not j.get("seen_after_hash") else "uncertain"))
        raise
    finally:
        bot.DELIVERY_GUARD = None


def acknowledge(queue, job_id):
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("회차 ID 오류")
    path = "jobs/" + job_id + ".json"
    job, sha = queue.get(path)
    if not job or job.get("status") != "delivery_complete":
        raise RuntimeError("전송 확인·처리 이력 커밋 전 종결 금지")
    if job.get("seen_after_hash") != seen_hash():
        raise RuntimeError("전송 후 처리 이력 불일치 — 종결 금지")
    job.update(status="completed", completed_at=now().isoformat())
    queue.put(path, job, sha)


def rollback_check(queue):
    index, _ = queue.get("index.json")
    for job_id in (index or {}).get("jobs", []):
        job, _ = queue.get("jobs/" + job_id + ".json")
        if job and job.get("status") in {"pending", "sending", "uncertain", "delivery_complete"}:
            raise RuntimeError("발송·처리 이력 미확정 회차 존재 — API 롤백 금지")
    print("미확정 전송 없음. 예약 정지→pending 회차 종결→기존 처리 이력 보존→명시적 API 복귀 순서")


def cancel(config, queue, job_id):
    if config["engine"] != "paused" or not JOB_ID.fullmatch(job_id):
        raise RuntimeError("일시중지 상태에서만 미발송 회차 취소 가능")
    path = "jobs/" + job_id + ".json"
    job, sha = queue.get(path)
    if not job or job.get("status") != "pending" or job.get("receipts"):
        raise RuntimeError("발송 시작·미확인 회차는 자동 취소 금지")
    job.update(status="cancelled", cancelled_at=now().isoformat())
    queue.put(path, job, sha)


def recover_state(config, queue, job_id):
    """SMTP 재발송 없이 접수 후 커밋에 실패한 이력만 복구한다."""
    if config["engine"] != "paused" or not JOB_ID.fullmatch(job_id):
        raise RuntimeError("일시중지 상태에서만 접수 후 이력 복구 가능")
    job, _ = queue.get("jobs/" + job_id + ".json")
    if not job or job.get("status") != "delivery_complete":
        raise RuntimeError("접수 완료 회차만 이력 복구 가능")
    current = seen_hash()
    if current == job.get("seen_after_hash"):
        return
    if current != job["input"]["seen_hash"]:
        raise RuntimeError("다른 처리 이력 존재 — 덮어쓰기 금지")
    snapshot = job.get("seen_after_snapshot", "")
    json.loads(snapshot)
    if hashlib.sha256(snapshot.encode()).hexdigest() != job.get("seen_after_hash"):
        raise RuntimeError("접수 후 이력 백업 불일치")
    temp = Path("seen_articles.json.subscription-tmp")
    temp.write_text(snapshot, encoding="utf-8")
    temp.replace("seen_articles.json")
    print("접수 후 처리 이력 복구; SMTP0/API0 — 커밋·ack 필요")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["scheduled", "collect-test", "consume", "ack", "connection", "initialize", "rollback-check", "cancel", "recover-state"])
    parser.add_argument("--job-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    if config["engine"] not in {"api", "subscription", "paused"}:
        raise RuntimeError("알 수 없는 운영 모드")
    if args.operation == "scheduled" and config["engine"] == "api":
        import monitor_v2
        monitor_v2.main()
        return
    if args.operation == "scheduled" and config["engine"] == "paused":
        print("운영 일시중지 — API/수집/발송0")
        return
    queue = Queue(config)
    if args.operation == "initialize":
        queue.initialize()
    queue.preflight()
    if args.operation in {"connection", "initialize"}:
        queue.preflight()
        path = "checks/" + str(os.environ.get("GITHUB_RUN_ID") or now().strftime("%Y%m%dT%H%M%S")) + ".json"
        probe = {"checked_at": now().isoformat(), "read_write": True, "paid_ai_calls": 0}
        queue.put(path, probe)
        if queue.get(path)[0] != probe:
            raise RuntimeError("비공개 저장소 쓰기 후 읽기 검증 실패")
        print("비공개 저장소 읽기·쓰기 검수 완료; API0/발송0")
    elif args.operation in {"scheduled", "collect-test"}:
        collect(config, queue, "verification" if args.operation == "collect-test" else "production")
    elif args.operation == "consume":
        consume(config, queue, args.job_id or "", args.dry_run, args.self_test)
    elif args.operation == "ack":
        acknowledge(queue, args.job_id or "")
    elif args.operation == "cancel":
        cancel(config, queue, args.job_id or "")
    elif args.operation == "recover-state":
        recover_state(config, queue, args.job_id or "")
    else:
        queue.preflight()
        rollback_check(queue)


def cli():
    try:
        main()
    except Exception as error:
        detail = str(error)
        public_messages = {
            "INSIGHT_QUEUE_TOKEN 미등록 — 비공개 저장소 연결 불가",
            "대기 저장소 접근 실패 또는 비공개가 아님",
            "전용 대기 브랜치 없음",
            "비공개 저장소 쓰기 후 읽기 검증 실패",
        }
        # 큐 API의 HTTP 상태와 고정된 연결 오류만 공개한다. SMTP/기사 세부는 알림에만 전달.
        if detail not in public_messages and not re.fullmatch(
                r"비공개 대기 저장소 (?:GET|PUT|POST) 실패 HTTP [0-9]{3}", detail):
            detail = "상세 오류는 담당자 알림과 비공개 회차를 확인하세요."
        print("인사이트 구독 경로 보류:", type(error).__name__, detail)
        notify_error(error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())

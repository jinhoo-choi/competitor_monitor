"""Source-backed event identity and sent-fact history; no network or model calls.

Company/domain/partner alone is never a suppression key. Legacy keys remain
readable; only exact audited phone-history aliases share a migration identity.
"""
import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RETENTION = timedelta(days=7)
STAGES = {"plan", "launched", "approved", "contracted", "completed", "cancelled", "other"}
COMPANY_ALIASES = {
    "삼성": "삼성증권", "삼성증권(주)": "삼성증권", "키움": "키움증권", "키움證": "키움증권",
    "KB": "KB증권", "국민증권": "KB증권", "KB Securities": "KB증권", "메리츠": "메리츠증권",
    "신한": "신한투자증권", "신한투자": "신한투자증권", "신한금융투자": "신한투자증권", "신한證": "신한투자증권",
    "NH": "NH투자증권", "나무증권": "NH투자증권", "NH투자": "NH투자증권",
    "미래에셋": "미래에셋증권", "미래에셋대우": "미래에셋증권", "토스": "토스증권",
    "BNK": "BNK투자증권", "BNK증권": "BNK투자증권", "DB금융투자": "DB증권", "교보": "교보증권",
}
PHONE_SUBJECT = "휴대폰개통이력점검"
PHONE_LEGACY = {"개통이력사기예방", "보이스피싱점검", "개통이력점검"}
TRACKING_PARAMS = {"fbclid", "gclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


def company_name(value):
    value = (value or "").strip()
    return COMPANY_ALIASES.get(value, value)


def normalize_text(value):
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = value.replace("휴대전화", "휴대폰").replace("핸드폰", "휴대폰")
    return re.sub(r"[^가-힣a-z0-9]", "", value)


def normalize_url(url):
    """Remove known tracking parameters, preserving article-identity query keys."""
    if not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return url
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                           urlencode(sorted(query)), ""))
    except ValueError:
        return url


def article_source(article):
    if article.get("_body_failed"):
        return article.get("description", "") or ""
    return " ".join(article.get(k, "") or "" for k in ("title", "description", "_body"))


def _subject(value):
    value = normalize_text(value)
    if value in {PHONE_SUBJECT, PHONE_SUBJECT + "서비스", "휴대폰개통이력조회", "휴대폰개통이력조회서비스"}:
        return PHONE_SUBJECT
    return value


def _legacy_identity(key):
    """Ignore processing-month drift for precise keys, never broad :: fallbacks."""
    if not isinstance(key, str) or "::" in key:
        return "", ""
    match = re.fullmatch(r"(.+?)_(.+)_\d{6}", key)
    if not match:
        return "", ""
    company, subject = company_name(match[1]), normalize_text(match[2])
    if company == "신한투자증권" and subject in PHONE_LEGACY:
        subject = PHONE_SUBJECT
    return company, subject


def _event_id(company, subject, action="", event_date=""):
    payload = [company_name(company), _subject(subject), normalize_text(action), event_date]
    return "evt_" + hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:24]


def _at(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_event_records(raw, now):
    """Exact timestamps for v1; conservative end-of-day migration for old keys."""
    if not isinstance(raw, dict):
        return []
    records = []
    for value in raw.get("event_records", []):
        if not isinstance(value, dict):
            continue
        stamp = _at(value.get("last_sent_at"))
        if (stamp and now - RETENTION <= stamp <= now and isinstance(value.get("event_id"), str)
                and isinstance(value.get("company"), str) and isinstance(value.get("facts", []), list)):
            records.append(copy.deepcopy(value))
    represented = {key for r in records for key in r.get("legacy_keys", [])}
    raw_events = raw.get("events", {})
    if isinstance(raw_events, dict):
        for day, keys in raw_events.items():
            stamp = _at(str(day) + "T23:59:59" + now.strftime("%z"))
            if stamp is None or stamp < now - RETENTION or stamp.date() > now.date():
                continue
            for key in keys if isinstance(keys, list) else []:
                company, subject = _legacy_identity(key)
                if not subject or key in represented:
                    continue
                records.append({"event_id": _event_id(company, subject), "company": company,
                    "subject": subject, "action": "", "event_date": "", "stage": "other", "facts": [],
                    "legacy_keys": [key], "first_sent_at": min(stamp, now).isoformat(),
                    "last_sent_at": min(stamp, now).isoformat(), "urls": [], "title": "", "legacy": True})
    return merge_event_records([], records, now)


def merge_event_records(existing, new, now):
    merged = {}
    for record in list(existing or []) + list(new or []):
        if not isinstance(record, dict) or not record.get("event_id"):
            continue
        record = copy.deepcopy(record)
        stamp = _at(record.get("last_sent_at"))
        if stamp is None or stamp < now - RETENTION or stamp > now:
            continue
        old = merged.get(record["event_id"])
        if old:
            facts = {normalize_text(f["key"]): f for f in old.get("facts", [])}
            facts.update({normalize_text(f["key"]): f for f in record.get("facts", [])})
            record["facts"] = list(facts.values())
            history = _fact_history(old)
            for key, values in _fact_history(record).items():
                history[key] = sorted(set(history.get(key, []) + values))
            record["fact_history"] = history
            for field in ("legacy_keys", "urls", "reported_values"):
                record[field] = sorted(set(old.get(field, []) + record.get(field, [])))
            record["identity_evidence"] = old.get("identity_evidence", []) + [
                proof for proof in record.get("identity_evidence", [])
                if proof not in old.get("identity_evidence", [])]
            record["first_sent_at"] = min(old["first_sent_at"], record["first_sent_at"])
            if _at(old["last_sent_at"]) > stamp:
                record["last_sent_at"] = old["last_sent_at"]
            record["legacy"] = old.get("legacy", False) and record.get("legacy", False)
        merged[record["event_id"]] = record
    # Never silently drop unexpired facts because of an arbitrary record cap.
    return sorted(merged.values(), key=lambda r: (r["last_sent_at"], r["event_id"]))


def _literal(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).lower()


def _literal_fact_present(value, quote):
    value, quote = _literal(value), _literal(quote)
    pattern = re.escape(value)
    if re.match(r"[+\-−]?\d", value):
        pattern = r"(?<![\d.,+\-−])" + pattern
    if value and value[-1].isdigit():
        pattern += r"(?![\d.])"
    return re.search(pattern, quote) is not None


def _numbers(value):
    """Canonical numbers WITH units/signs: 5.0%=5%, 100명 != 100만원."""
    result = set()
    for number, unit in re.findall(r"([+\-]?\d+(?:\.\d+)?)\s*(억원|만원|천원|조원|억|만|조|천|원|명|건|좌|개|%|년|월|일|달러)?",
                                   value.replace(",", "").replace("−", "-")):
        result.add(str(Decimal(number).normalize()) + (unit or ""))
    return result


def _stage_evidence(event):
    patterns = {
        "launched": r"출시|개시|오픈|도입|가동", "approved": r"승인|인가|허가",
        "contracted": r"체결|본계약|계약서명", "completed": r"완료|완공|종결",
        "cancelled": r"취소|철회|중단|폐지|무산",
    }
    pattern = patterns.get(event["stage"])
    if not pattern:
        return True
    for fact in event["facts"]:
        evidence = fact["evidence_quote"]
        if re.search(r"(?:" + pattern + r").{0,16}(?:예정|계획|추진|검토|않|못|아니|미정)", evidence):
            continue
        if event["stage"] != "cancelled" and re.search(r"(?:" + pattern + r").{0,16}(?:취소|무산)", evidence):
            continue
        if re.search(r"(?:미승인|미완료|미체결|미출시|미취소|아직.{0,12}(?:않|못))", evidence):
            continue
        if re.search(pattern, evidence):
            return True
    return False


def _fact_history(record):
    history = copy.deepcopy(record.get("fact_history", {}))
    for fact in record.get("facts", []):
        key = normalize_text(fact["key"])
        history[key] = sorted(set(history.get(key, []) + [fact["value"]]))
    return history


def _equivalent_value(left, right):
    if _literal(left) == _literal(right):
        return True
    numbers = _numbers(left)
    return bool(numbers) and numbers == _numbers(right)



def _fact_numbers(key, value):
    # Count nouns for a count axis are not a change from "4 firms" to "4 places".
    # Currency, percentages, dates, signs and other units remain distinct.
    if re.search(r"참여기업|지원팀|통과|결선", normalize_text(key)):
        value = re.sub(r"(개사|곳|팀)", "개", value)
    return _numbers(value)


def _fact_changes(event, prior):
    old = {normalize_text(f["key"]): f["value"] for f in prior.get("facts", [])}
    all_old = {_literal(v) for v in old.values()}
    reported = set(prior.get("reported_values", []))
    changes, history = [], _fact_history(prior)
    for fact in event["facts"]:
        key, value = normalize_text(fact["key"]), fact["value"]
        if (_literal(old.get(key, "")) == _literal(value) or _literal(value) in all_old
                or (re.search(r"개최일|행사일|결선일|계약일|발생일", key)
                    and _same_partial_date(old.get(key, ""), value) is True)):
            continue
        if re.search(r"(?:변경|인상|인하|확대|축소|전환|조정|도입).{0,12}(?:않|못|아니|없)", fact["evidence_quote"]):
            continue
        previous_value_reported = any(
            _equivalent_value(value, previous) or (
                bool(_fact_numbers(key, value))
                and _fact_numbers(key, value) == _fact_numbers(key, previous))
            for previous in history.get(key, []))
        reversal = (re.search(r"다시|재차|원복|되돌|재인하|재인상", fact["evidence_quote"])
                    and re.search(r"변경|낮췄|높였|인하|인상|되돌|원복", fact["evidence_quote"])
                    and not re.search(r"예정|계획|검토|않|못|아니", fact["evidence_quote"]))
        if previous_value_reported and not reversal:
            continue
        numbers = _fact_numbers(key, value)
        if numbers:
            previous_numbers = _fact_numbers(key, old.get(key, ""))
            if previous_numbers and numbers != previous_numbers:
                changes.append(key)
            elif not old.get(key) and not numbers <= reported:
                changes.append(key)
        elif (event["stage"] != prior.get("stage", "other")
                and prior.get("stage") not in {None, "other"}
                and event["stage"] not in {"other", "plan"}
                and _stage_evidence(event)
                and re.search(r"출시|개시|오픈|승인|인가|체결|완료|취소|철회|중단", fact["value"])):
            changes.append(key)
        elif (old.get(key) and re.search(r"변경|확대|축소|폐지|취소|철회|중단", fact["value"])
                and not re.search(r"예정|계획|검토|않|못", fact["evidence_quote"])):
            changes.append(key)
    return sorted(set(changes))


def validate_event(article, analysis, recent_events):
    """Validate model metadata against captured source and previous sent facts."""
    event = copy.deepcopy(analysis.get("event"))
    if not isinstance(event, dict):
        raise ValueError("사건 비교 정보 누락")
    for field, bound in {"subject": 100, "action": 60, "event_date": 10,
                         "matched_event_id": 80, "stage": 16, "novelty": 8}.items():
        value = event.get(field)
        if not isinstance(value, str) or len(value) > bound:
            raise ValueError("사건 필드 형식 오류: " + field)
    if not event["subject"].strip() or not event["action"].strip():
        raise ValueError("구체적 사건 주제·행위 누락")
    if event["stage"] not in STAGES or event["novelty"] not in {"new", "same", "update"}:
        raise ValueError("사건 단계·신규성 오류")
    source = article_source(article)
    if _subject(event["subject"]) not in normalize_text(source):
        raise ValueError("원문에 없는 사건 주제")
    if event["event_date"]:
        try:
            parsed = datetime.strptime(event["event_date"], "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != event["event_date"]:
                raise ValueError()
        except ValueError:
            raise ValueError("실제 사건 날짜 형식 오류") from None
        forms = (event["event_date"], f"{parsed.year}년{parsed.month}월{parsed.day}일",
                 f"{parsed.year}.{parsed.month}.{parsed.day}", f"{parsed.year}/{parsed.month}/{parsed.day}")
        if not any(form in _literal(source) for form in forms):
            raise ValueError("원문에 없는 실제 사건 날짜")
    facts = event.get("facts")
    if not isinstance(facts, list) or not 1 <= len(facts) <= 16:
        raise ValueError("사건 사실 목록 누락·초과")
    keys = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("사건 사실 형식 오류")
        for field, bound in {"key": 60, "value": 160, "evidence_quote": 500}.items():
            if not isinstance(fact.get(field), str) or not fact[field].strip() or len(fact[field]) > bound:
                raise ValueError("사건 사실 필드 오류: " + field)
        key = normalize_text(fact["key"])
        if not key or key in keys:
            raise ValueError("사건 사실 축 중복")
        keys.add(key)
        if (len(fact["evidence_quote"]) < 8 or fact["evidence_quote"] not in source
                or not _literal_fact_present(fact["value"], fact["evidence_quote"])):
            raise ValueError("사건 사실 원문 근거 불일치")
    if not _stage_evidence(event):
        raise ValueError("사건 단계의 실제 원문 근거 없음 (예정·부정문 확인)")
    matched = event["matched_event_id"]
    prior = next((r for r in recent_events if r.get("event_id") == matched), None) if matched else None
    if event["novelty"] == "new":
        if matched:
            raise ValueError("신규 사건에 기존 사건 ID 지정")
    elif not prior or company_name(prior.get("company")) != company_name(analysis.get("company_name")):
        raise ValueError("기존 사건 ID·회사 불일치")
    if prior and not prior.get("legacy") and (_date_conflict(prior, event) or (
            _subject(prior.get("subject")) != _subject(event["subject"])
            and not same_event_identity(prior, _identity_record(article, analysis, event)))):
        raise ValueError("기존 사건과 다른 서비스·계약 주제")
    if prior and prior.get("legacy") and prior.get("subject") == PHONE_SUBJECT and _subject(event["subject"]) != PHONE_SUBJECT:
        raise ValueError("기존 서비스와 다른 사건 주제")
    if event["novelty"] == "update" and not _fact_changes(event, prior):
        raise ValueError("기존 발송 대비 확인된 신규 사실 없음")
    return event


def _fallback_event(article, analysis):
    company = company_name(analysis.get("company_name") or article.get("_company"))
    legacy_company, legacy_subject = _legacy_identity(analysis.get("event_key", ""))
    title = normalize_text(article.get("title", ""))
    if re.search(r"통화패턴.*(?:ai|인공지능)|(?:ai|인공지능).*통화패턴", title):
        subject = "보이스피싱통화패턴AI분석"
    elif (legacy_subject == PHONE_SUBJECT or PHONE_SUBJECT in normalize_text(
            article.get("title", "") + " " + article.get("description", ""))):
        subject = PHONE_SUBJECT
    else:
        subject = legacy_subject if legacy_company == company else ""
    if not subject:
        subject = normalize_text(article.get("title", ""))
    return {"subject": subject, "action": "", "event_date": "", "stage": "other",
            "facts": [], "matched_event_id": "", "novelty": "new"}


def assess_event(article, analysis, records, legacy_events, now):
    """Explainable decision and candidate record; never writes history."""
    company = company_name(analysis.get("company_name") or article.get("_company"))
    event = copy.deepcopy(analysis.get("event")) or _fallback_event(article, analysis)
    structured = bool(analysis.get("event"))
    if structured:
        validation_analysis = analysis
        matched_record = next((r for r in records if r.get("event_id") == event.get("matched_event_id")), None)
        if event.get("novelty") == "update" and matched_record and not _fact_changes(event, matched_record):
            # A prior card in this batch may already contain this exact update.
            validation_analysis = copy.deepcopy(analysis)
            validation_analysis["event"]["novelty"] = "same"
        event = validate_event(article, validation_analysis, records)
    subject, action = _subject(event["subject"]), normalize_text(event["action"])
    if subject == PHONE_SUBJECT:
        action = ""
    legacy_key = analysis.get("event_key", "")
    legacy_identity = _legacy_identity(legacy_key)
    prior = None
    if event.get("matched_event_id"):
        prior = next((r for r in records if r["event_id"] == event["matched_event_id"]
                      and company_name(r["company"]) == company), None)
    if prior is None:
        for candidate in reversed(records):
            if company_name(candidate.get("company")) != company:
                continue
            same_subject = _subject(candidate.get("subject")) == subject
            same_action = normalize_text(candidate.get("action", "")) == action
            date_conflict = _date_conflict(candidate, event)
            same_legacy = (any(_legacy_identity(k) == legacy_identity for k in candidate.get("legacy_keys", []))
                           if legacy_identity[1] else False)
            if ((same_subject and (same_action or candidate.get("legacy")) and not date_conflict)
                    or (structured and same_event_identity(candidate, _identity_record(article, analysis, event)))
                    or (same_legacy and not structured)):
                prior = candidate
                break
    legacy_hit = False
    if prior is None:
        for key in legacy_events or []:
            old_company, old_subject = _legacy_identity(key)
            if old_company == company and old_subject and (old_subject == subject or (not structured and old_subject == legacy_identity[1])):
                legacy_hit = True
                break
    novelty, reason = "new", "new_specific_event"
    if prior:
        changes = _fact_changes(event, prior) if structured else []
        if structured and event["novelty"] != "same" and changes:
            novelty, reason = "update", "source_backed_fact_change:" + ",".join(changes)
        else:
            novelty, reason = "same", "same_event_no_new_facts"
    elif legacy_hit:
        novelty, reason = "same", "legacy_precise_event_match"
    event_id = prior["event_id"] if prior else _event_id(company, subject, action, event.get("event_date", ""))
    record = {"event_id": event_id, "company": company, "subject": event["subject"],
        "action": "" if subject == PHONE_SUBJECT else event["action"], "event_date": event.get("event_date", ""),
        "stage": event["stage"], "facts": event["facts"], "legacy_keys": [legacy_key] if legacy_key else [],
        "first_sent_at": prior["first_sent_at"] if prior else now.isoformat(), "last_sent_at": now.isoformat(),
        "urls": [normalize_url(article.get("link", ""))] if article.get("link") else [],
        "title": article.get("title", "")[:300], "legacy": False,
        "reported_values": sorted(_numbers(article_source(article))),
        "fact_history": _fact_history({"facts": event["facts"]})}
    return {"duplicate": novelty == "same", "event_id": event_id, "novelty": novelty,
            "reason": reason, "prior_sent_at": prior["last_sent_at"] if prior else "", "record": record}



def _same_partial_date(left, right):
    def parts(value):
        value = _literal(value)
        match = re.search(r"(?:(\d{4})년)?(\d{1,2})월(\d{1,2})일", value)
        if match:
            return tuple(int(x) if x else None for x in match.groups())
        match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
        if match:
            return tuple(map(int, match.groups()))
        return None
    a, b = parts(left), parts(right)
    if a is None or b is None:
        return None
    return all(x is None or y is None or x == y for x, y in zip(a, b))


def _date_conflict(left, right):
    if left.get("event_date") and right.get("event_date") and left["event_date"] != right["event_date"]:
        return True
    # Partial dates still distinguish separate editions when the year is absent.
    right_facts = {normalize_text(f["key"]): f["value"] for f in right.get("facts", [])}
    for fact in left.get("facts", []):
        key = normalize_text(fact["key"])
        other = right_facts.get(key)
        if (other and re.search(r"개최일|행사일|결선일|계약일|발생일", key)
                and _same_partial_date(fact["value"], other) is False):
            return True
    return False



def _identity_fact_equal(left, right):
    if _literal(left) == _literal(right):
        return True
    if not _equivalent_value(left, right):
        return False
    # Numeric equality alone cannot erase geography/product names.
    def context(value):
        value = re.sub(r"[+\-−]?\d[\d,.]*", "", _literal(value))
        return value.replace("지원팀", "팀").replace("참가팀", "팀")
    return context(left) == context(right)


def _source_has_subject(source, subject):
    if re.fullmatch(r"[a-z]{2,6}", subject):
        return re.search(r"(?<![a-z0-9])" + re.escape(subject) + r"(?![a-z0-9])",
                         unicodedata.normalize("NFKC", source).lower()) is not None
    return subject in normalize_text(source)



def _identity_anchor_equal(key, left, right):
    return (_identity_fact_equal(left, right)
            or (re.search(r"개최일|행사일|결선일|계약일|발생일", normalize_text(key))
                and _same_partial_date(left, right) is True))


def _shared_identity_facts(left, right):
    old = {normalize_text(f["key"]): f["value"] for f in left.get("facts", [])}
    return [{"key": normalize_text(f["key"]), "value": f["value"]}
            for f in right.get("facts", [])
            if normalize_text(f["key"]) in old
            and _identity_anchor_equal(f["key"], old[normalize_text(f["key"])], f["value"])]


def _precise_shared_key(left, right):
    left_keys = {_legacy_identity(k) for k in left.get("legacy_keys", [])}
    return any(identity[1] and identity in left_keys
               for identity in (_legacy_identity(k) for k in right.get("legacy_keys", [])))


def same_event_identity(left, right):
    """Source-witnessed aliases, scoped by company/action/key and corroborating facts.

    A common company, partner, theme, count, or model key never suffices alone.
    Persisted alias evidence is rechecked, never promoted to a global synonym.
    """
    if company_name(left.get("company")) != company_name(right.get("company")):
        return False
    if _date_conflict(left, right):
        return False
    if normalize_text(left.get("action", "")) != normalize_text(right.get("action", "")):
        return False
    a, b = _subject(left.get("subject")), _subject(right.get("subject"))
    if a == b:
        return True
    if not a or not b or not _precise_shared_key(left, right):
        return False
    shared = _shared_identity_facts(left, right)
    if len(shared) < 2:
        return False
    graph = {}
    for record in (left, right):
        source = record.get("_identity_source", "")
        if (not re.search(r"달리|비교|별개|다른|각각|별도", source)
                and _source_has_subject(source, a) and _source_has_subject(source, b)):
            return True
        for proof in record.get("identity_evidence", []):
            subjects, anchors = proof.get("subjects", []), proof.get("anchors", [])
            if len(subjects) != 2:
                continue
            matched = {anchor["key"] for anchor in anchors if any(
                anchor["key"] == fact["key"] and _identity_anchor_equal(anchor["key"], anchor["value"], fact["value"])
                for fact in shared)}
            if len(matched) >= 2:
                graph.setdefault(subjects[0], set()).add(subjects[1])
                graph.setdefault(subjects[1], set()).add(subjects[0])
    pending, visited = [a], set()
    while pending:
        subject = pending.pop()
        if subject == b:
            return True
        if subject not in visited:
            visited.add(subject)
            pending.extend(graph.get(subject, set()) - visited)
    return False


def _identity_record(article, analysis, event):
    return {"company": company_name(analysis.get("company_name") or article.get("_company")),
            "subject": event["subject"], "action": event["action"],
            "event_date": event.get("event_date", ""), "facts": event.get("facts", []),
            "legacy_keys": [analysis.get("event_key", "")],
            "_identity_source": article_source(article)}


def select_event_cards(articles, records, legacy_events, now):
    """One card per proven event, assessed only against immutable sent history.

    Mutates only candidate article decision metadata. Never sends or saves.
    Contradictory same-batch numeric facts are withheld: publication time alone
    cannot prove the chronology of an underlying event.
    """
    entries = []
    for article in articles:
        analysis = article.get("analysis")
        if not analysis:
            continue
        decision = assess_event(article, analysis, records, legacy_events, now)
        article["_event_decision"] = {key: decision.get(key, "") for key in
                                     ("prior_sent_at", "novelty")}
        article["_event_decision"].update(matched_event_id=decision["event_id"],
                                         dedup_reason=decision["reason"])
        record = copy.deepcopy(decision["record"])
        record["_identity_source"] = article_source(article)
        entries.append((article, decision, record))
    groups = [{i} for i in range(len(entries))]
    # Build witnessed links first; then reject contradictory transitive bridges.
    links = [(i, j) for i in range(len(entries)) for j in range(i)
             if same_event_identity(entries[i][2], entries[j][2])]
    for i, j in sorted(links):
        left = next(g for g in groups if i in g)
        right = next(g for g in groups if j in g)
        if left is right:
            continue
        if any(_date_conflict(entries[a][2], entries[b][2]) for a in left for b in right):
            raise ValueError("동일 회차 사건 날짜 충돌 — 발송 보류")
        left.update(right)
        groups.remove(right)
    selected = []
    for group in groups:
        historical_ids = {entries[i][1]["event_id"] for i in group if entries[i][1].get("prior_sent_at")}
        if len(historical_ids) > 1:
            raise ValueError("동일 회차 사건 이력 ID 충돌 — 발송 보류")
        if len({_subject(entries[i][2]["subject"]) for i in group}) > 1:
            if any(not _precise_shared_key(entries[i][2], entries[j][2])
                   for i in group for j in group):
                raise ValueError("동일 회차 사건 키 충돌 — 발송 보류")
        # An alias witness in another current article can resolve a history match.
        # Reassess novelty against that SENT baseline, never merely copy its ID.
        if historical_ids:
            historical_id = next(iter(historical_ids))
            prior = next(r for r in records if r["event_id"] == historical_id)
            for i in group:
                current, decision, record = entries[i]
                changes = _fact_changes(record, prior)
                claimed_same = current["analysis"].get("event", {}).get("novelty") == "same"
                novelty = "update" if changes and not claimed_same else "same"
                reason = ("source_backed_fact_change:" + ",".join(changes)
                          if novelty == "update" else "same_event_no_new_facts")
                decision.update(event_id=historical_id, duplicate=novelty == "same",
                                novelty=novelty, reason=reason, prior_sent_at=prior["last_sent_at"])
                record.update(event_id=historical_id, first_sent_at=prior["first_sent_at"])
                current["_event_decision"].update(matched_event_id=historical_id,
                    prior_sent_at=prior["last_sent_at"], novelty=novelty, dedup_reason=reason)
        eligible = [i for i in group if not entries[i][1]["duplicate"]]
        if not eligible:
            continue
        # A key may be differently worded; only comparable numeric axes conflict.
        values, date_values = {}, {}
        for i in eligible:
            for fact in entries[i][2].get("facts", []):
                key = normalize_text(fact["key"])
                nums = _fact_numbers(key, fact["value"])
                if re.search(r"개최일|행사일|결선일|계약일|발생일", key):
                    if key in date_values and _same_partial_date(date_values[key], fact["value"]) is True:
                        continue
                    date_values[key] = fact["value"]
                if nums:
                    if key in values and values[key] != nums:
                        raise ValueError("동일 회차 사건 수치 충돌 — 발송 보류: " + key)
                    values[key] = nums
        # Pick the most informative already-written card, stably across input order.
        winner = min(eligible, key=lambda i: (
            -float(entries[i][0]["analysis"].get("impact_score", 0)),
            -len(entries[i][2].get("facts", [])),
            entries[i][0].get("article_id") or normalize_url(entries[i][0].get("link", ""))))
        article, decision, record = entries[winner]
        event_id = next(iter(historical_ids)) if historical_ids else min(entries[i][1]["event_id"] for i in group)
        record["event_id"] = event_id
        # Preserve only the representative's facts/URL. Alias provenance is identity
        # metadata, never evidence that discarded article details were delivered.
        proofs = list(record.get("identity_evidence", []))
        for i, j in links:
            if i not in group or j not in group:
                continue
            a, b = entries[i][2], entries[j][2]
            if _subject(a["subject"]) != _subject(b["subject"]):
                proof = {"subjects": sorted({_subject(a["subject"]), _subject(b["subject"])}),
                         "anchors": _shared_identity_facts(a, b)}
                if proof not in proofs:
                    proofs.append(proof)
        if proofs:
            record["identity_evidence"] = proofs
        record.pop("_identity_source", None)
        article["_event_record"] = record
        article["_event_decision"]["matched_event_id"] = event_id
        selected.append(article)
        for i in group:
            if i == winner:
                continue
            other, prior_decision, _ = entries[i]
            other["_event_decision"].update(
                matched_event_id=event_id, novelty="same",
                prior_sent_at=prior_decision.get("prior_sent_at", ""),
                dedup_reason="same_batch_event_consolidated")
    return [a for a in articles if any(a is picked for picked in selected)]

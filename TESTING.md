# 인사이트봇 표준 테스트 절차

> 코드 수정 후 **정기 실행(10:00 / 17:00 KST)에 반영되기 전** 반드시 이 절차를 거친다.
> 목적: 그룹(risk_aigent) 라이브 오발송 방지 + 실데이터 기반 회귀 검증.

---

## 0. 절대 원칙

| 금지 | 사유 |
|---|---|
| `eBiz 인사이트봇` 워크플로(`run_monitor.yml`) 수동 실행 | **전체 그룹에 라이브 발송됨** |
| 검증 없이 main 푸시 후 정기 실행 대기 | 오탐이 그대로 팀에 배포됨 |

테스트는 **`[TEST] 인사이트봇 — 본인 발송 전용`(`test_dispatch.yml`)만** 사용한다.

---

## 1. 절차 (5단계)

### STEP 1 — 오탐 근거 확보
발송된 실제 메일에서 증상을 특정한다. 이론적 추론이 아닌 **실메일 기반**.

```
Gmail 검색:  in:sent subject:인사이트 newer_than:7d
날짜 범위:   in:sent subject:인사이트 after:2026/08/01 before:2026/08/03
```

기록할 것: 발송 일시 / 카드 회사 태그 / 점수·등급 / 기사 제목 / 원문 대조 결과.

### STEP 2 — 수정 + 오프라인 회귀 테스트
실제 오탐 케이스 + **기존 정상 케이스**를 함께 mock으로 돌려 회귀 여부를 확인한다.
(정상 케이스 미포함 시 과잉 억제 사고 발생 — 레이어4 수치패턴 사례 참조)

```bash
python -m py_compile monitor_v2.py
python test_regress.py          # 오탐 N건 + 정상 N건 동시 검증
```

통과 기준: **오탐 케이스는 교정, 정상 케이스는 무변화.**

### STEP 3 — main 푸시
```
GitHub Contents API (Authorization 헤더 필수 — 없으면 rate limit으로 stale SHA 반환)
대용량 페이로드는 JSON 파일로 작성 후  -d @file.json
```

### STEP 4 — TEST_MODE 실행 ★핵심
**Actions → `[TEST] 인사이트봇 — 본인 발송 전용` → Run workflow**

TEST_MODE 4중 방어:

| 층 | 위치 | 동작 |
|---|---|---|
| 1 | `test_dispatch.yml` | `RECIPIENTS` / `RECIPIENTS_CC` 시크릿 **미주입** |
| 2 | `monitor_v2.py` 상단 | `TEST_MODE=1` → `RECIPIENTS_ALL=[]`, `RECIPIENTS_CC=[]` |
| 3 | `_smtp_send()` | `targets = [GMAIL_USER]` 강제 — 인자에 그룹이 와도 차단 |
| 4 | `save_seen()` | 즉시 return → **정기 실행 dedup 상태 오염 없음** |

제목에 `[TEST]` 프리픽스가 붙는다.

### STEP 5 — 수신 메일 검수
```
Gmail 검색:  in:sent subject:[TEST] newer_than:1d
```

체크리스트:
- [ ] 제목에 `[TEST]` 프리픽스가 붙었는가 (모든 라우팅 경로 공통)
- [ ] 수신자가 **본인 1인**인가 (`risk_aigent` 사본이 없어야 함)
- [ ] 수정 대상 항목이 의도대로 교정됐는가
- [ ] 기존 정상 카드가 그대로인가 (회귀 없음)
- [ ] `seen_articles.json` 커밋이 **발생하지 않았는가** (커밋 히스토리 확인)
- [ ] 필터 로그 아티팩트(`filter-log-<run_id>`)로 선별 과정 확인

### STEP 6 — 회귀 로그 등재
`FALSE_POSITIVES.md` 에 증상 / 근본원인 / 커밋 SHA / 검증 결과를 1행 추가한다.

---

## 2. 실행 이력 (템플릿)

| 일자 | 대상 커밋 | run_id | 결과 | 비고 |
|---|---|---|---|---|
| 2026-08-02 | `f3c708d` 귀속검증·라이선스하한·주체키 | `30742377374` | ✅ success | 두나무 기사 `삼성증권 8.2(상)` → `두나무 6.6(중)` 교정 확인. 본인 1통만 수신, seen 커밋 없음 |
| 2026-08-03 | `6890c1d4` 경쟁사 게이트 상위화 | `30780550292` | ✅ success | 수집 53 → 선별 8 → 탐지 3, **전건 증권사**(현대차·유안타·삼성). 비증권 주체 드롭 정상. 단 저영향 라우팅 제목에 `[TEST]` 누락 발견 → `ca9157e1`로 수정 |
| 2026-08-17 | `6451b120` 마일스톤 dedup + 업계동향 태그 | `31955666934` | ✅ success | 격리·프리픽스 정상. 단 심야 실행이라 수집 14건·탐지 0건으로 **신규 로직 미발동** — 실환경 검증은 다음 정기 실행분으로 이월 |

---

## 2-1. 테스트 실행 타이밍 주의

TEST_MODE는 **실행 시점의 뉴스**를 수집한다. 심야·주말에 돌리면 수집량이 10~20건대로 떨어져
탐지 0건이 나오고, 수정한 로직이 한 번도 타지 않은 채 success로 끝난다(2026-08-17 사례).
로직 발동까지 확인하려면 **평일 주간(09~18시)** 에 실행하거나, 오프라인 회귀 스위트
(`test_regress_all.py`)로 대체 검증한 뒤 다음 정기 실행분 메일로 최종 확인한다.

## 3. 알려진 환경 제약

| 항목 | 내용 |
|---|---|
| Actions 로그 | Azure blob 리다이렉트 → 외부 도구에서 직접 조회 불가. 스텝별 `conclusion`은 Jobs API로 확인 가능 |
| raw.githubusercontent | CDN 캐시 지연. 최신 내용은 **Contents API + base64 디코드** 사용 |
| `seen_articles.json` | `.gitignore` 차단 이력 있음 → 정기 워크플로는 `git add -f` 필수, Settings에서 Actions Read/Write 권한 확인 |

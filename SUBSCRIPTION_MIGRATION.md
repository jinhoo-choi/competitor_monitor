# 인사이트봇 구독 분석 전환

이 변경은 기존 네이버 수집·하드필터·귀속 검증·중복 제거·메일을 유지하고 AI 분석만 ChatGPT 예약으로 분리한다. 비공개 연결·실제 예약 실행·본인 발송 검수와 사용자 문구 검토를 통과했고, 2026-10-14 사용자 최종 승인에 따라 `subscription_config.json`의 engine을 **subscription**으로 설정한다. 리스크봇과 부정여론봇은 변경하지 않는다.

## 운영 연결

- 비공개 저장소: `jinhoo-choi/insight-bot-data`, 초기화된 `main` 브랜치. ChatGPT GitHub 연결에도 접근을 허용한다.
- `competitor_monitor` Actions secret: `INSIGHT_QUEUE_TOKEN`. 대상 비공개 저장소 Contents 읽기·쓰기만 허용하는 GitHub 접근 키다. Anthropic API 키와 용도가 다르다. 키를 채팅·소스·로그에 넣지 않는다.
- 공개 소스에는 opaque 회차 ID와 검수 여부만 저장한다. 기사 원문·분석·메일 미리보기·발송 영수증은 `subscription/insight/`에 비공개로 저장한다.
- 기존 정기 실행은 구독 모드에서 자료를 수집하고, ChatGPT 예약은 한국시간 10:10 / 18:10에 `subscription_task_prompt.txt`를 수행한다. 기존 10:00 / 18:00 수집보다 10분 늦다. 뉴스 창은 17시간으로 야간 16시간 간격을 포함한다.
- 예약이 GitHub 결과 생성·깨우기 파일 쓰기를 실제 수행해야 한다. 생성 API 성공만으로 연결 검수 완료라고 판단하지 않는다. 예약 쓰기 권한이나 사용량이 막히면 회차를 보류하고 API로 자동 전환하지 않는다.

## 검수와 전환

1. 기존 메일 회귀와 `test_subscription_bridge.py`를 오프라인에서 통과시킨다.
2. 개발 브랜치의 `subscription_request.json`에 operation=initialize를 기록해 등록된 PAT로 요청한 새 비공개 큐를 초기화한다. 기존 공개 저장소·다른 소유자 키는 사용하지 않는다. operation=connection은 기존 큐 읽기·쓰기만 검수한다. 이 단계는 유료 AI API·SMTP를 호출하지 않는다.
3. 검수 전용 회차(mode=verification)를 생성하고 실제 ChatGPT 예약이 private results JSON을 쓰게 한다. 분석 입력 ID, 해시, 정책 버전, 만료, 점수·등급, 원문 인용, 수치, 중·상 재검토를 검증한다.
4. 동일 JSON을 이용한 검수 요청은 `self_test=true`로 제한한다. SMTP는 `TEST_MODE=1`의 기존 4중 방어로 본인에게만 보내고 seen 이력은 저장하지 않는다. 결과 없는 검수는 `dry_run=true`로 미리보기만 비공개 저장한다. 별개의 회차를 사용한다.
5. 스키마·규칙·메일 렌더링과 본인 SMTP 서버 접수, seen 무변경을 확인한 뒤 main에 병합한다. 예약 프롬프트를 main 대상으로 확정하고 engine=subscription으로 명시적으로 전환한다.
6. 첫 자연 정기 회차의 private status=completed, Actions 성공, 유료 API 호출0을 확인한다. SMTP accepted는 서버 접수 증거다. 받은편지함 도착은 실제 수신 메일로 별도 확인한다.

## 오류 보류

TTL 120분이 지난 결과, 입력 해시·처리 이력 변경, 전체 ID 누락·중복, 허위 인용·수치, 점수 불일치, 미확인 수신자 접수는 발송·이력 종결을 막는다. 구독 모드에서는 `_ai_create`가 차단되고 소비 워크플로에 유료 AI 키를 주입하지 않는다. 헤더는 확정된 카드 건수로 생성한다.

각 수신자는 보내기 전에 sending을 기록하고 서버 접수 후 accepted를 기록한다. 접수 여부를 모르면 uncertain으로 보류하고 자동 재시도하지 않는다. 영수증은 주소의 SHA256을 키로 사용한다. 이미 접수된 주소는 재발송하지 않는다. 공유 Actions concurrency로 수집·소비의 처리 이력 충돌을 줄이고, 코드가 처리 이력을 저장하기 전에 모든 영수증을 확인한다.

## 롤백

기준 코드: `7f026d94808140b1ba6e69968f7f38e0cff78dc2`. 기준 코드는 롤백 브랜치에 보존한다. **저장소 전체 reset/revert로 최신 seen_articles.json을 되돌리지 않는다.**

1. `subscription_config.json`의 engine을 paused로 바꾸고 인사이트 ChatGPT 예약을 중지한다. 기존 정기 실행도 API·수집·발송을 멈춘다.
2. pending이며 receipts가 비어 있는 회차만 `python subscription_bridge.py cancel --job-id ID`로 종결한다. 공개 요청 파일 operation=cancel로 실행할 수도 있다. sending/uncertain은 실제 SMTP 접수와 영수증을 확인하기 전 취소·재발송하지 않는다.
3. delivery_complete인데 source seen 커밋만 실패한 경우 paused 상태에서 `python subscription_bridge.py recover-state --job-id ID`로 비공개 백업을 복구한다. 공개 요청 operation=recover-state는 메일을 보내지 않고 이력 커밋과 ack만 수행한다. 현재 이력이 수집 당시 또는 접수 후 해시와 다르면 덮어쓰기를 막는다.
4. `python subscription_bridge.py rollback-check`가 미종결 회차 없이 통과했는지 확인한다. pending도 차단 대상이다.
5. 최신 seen을 유지한 채 engine=api로 바꾼다. 기존 Anthropic 키와 이전 판정 경로를 사용한다. 이 단계부터 API 비용이 다시 발생하며 자동 오류 폴백으로 수행하지 않는다.

구독의 모델명·사용량은 예약 실행에서 확인된 값만 기록한다. 모델명 미확인은 unconfirmed로 남기며 구독 사용량을 API 토큰 비용처럼 추정해 확정하지 않는다.

## 2026-10-14 전환 검수 기록

- 실제 예약 분석·본인 SMTP 검수: https://github.com/jinhoo-choi/competitor_monitor/actions/runs/38486333784
- 보완 문구 본인 SMTP 검수: https://github.com/jinhoo-choi/competitor_monitor/actions/runs/38490114372 — private verified/self_test=true, 본인 accepted 1건, 유료 AI API0, seen 무변경. 사용자가 발신함의 문구를 검토해 승인했다. 실제 받은편지함 도착과 SMTP 접수는 구별한다.
- 오프라인 회귀 32건과 GitHub CI 통과. 첫 자연 운영 회차의 completed와 운영 수신자 영수증은 별도 확인한다.
- 요약은 행위·서비스명·원문 수치·조건을 보존한 두 문장 이내, 60~80자 권장·최대 90자다. 대응방안은 구체적인 비교 항목과 다음 점검을 담은 두 문장·80자 이내다. 실제 발행어음의 본문 확보·귀속·영향 사업 검증을 통과하면 최소 6.5점을 유지한다.
- 공개 연결 요청은 메타데이터만 담은 connection으로 두고 main에서 비공개 읽기·쓰기 연결을 재확인한다. 운영 메일은 10시/18시 수집 이후 10:10/18:10 구독 분석과 소비가 끝난 뒤 발송된다. 구독 사용량 부족이나 오류에서 자동 API 전환하지 않는다.

기사 본문 추출은 매칭된 태그의 내부 텍스트만 읽고 HTML 엔티티를 복원한다. 스크립트/스타일의 HTML 템플릿을 제외하고 짧은 페이지 컨트롤 영역을 건너뛰어 실제 기사 문단을 찾는다. 본문을 확보하지 못하면 description과 본문 미수집 표시로 보수적으로 분석한다.

# Expired-input recovery (offline-tested, not yet deployed)

## Contract

Risk's preserve-before-expire continuity design is applied to Insight's existing
immutable job inputs. The old input/result and TTL are never rewritten. The old
job status alone changes to `expired`, atomically with a fresh pending job and
rolling index. Only production pending/expired inputs with an empty receipt map
and no delivery/history/recipient-plan evidence qualify. `sending_at` alone is a
consume reservation; pre-DATA failures may leave it without any SMTP attempt.

Collection reads every retained `jobs/*.json` from one pinned Git commit rather
than trusting the 60-job display index. Truncated trees, missing indexed jobs,
source/hash/body/identity errors and unresolved unrelated jobs fail closed.
Git tree+commit creation followed by a non-force ref update publishes one atomic
change. A sibling writer makes the final update fail. Writes with uncertain
responses are never retried automatically. A new full snapshot reconciles them.
The existing owner-only expiry review notice remains best-effort, once for each
newly expired job after confirmed atomic publication. It does not claim delivery.
Do not prune old job inputs or completed recovery evidence without a replacement
verified archival ledger; they are the durable recovery store.

Fresh production collection merges aged unprocessed candidates, even beyond the
17-hour collection window. Frozen source articles retain all fields and body.
Current policy/seen are rechecked. Current/batch duplicate omissions remain
explicitly recorded and unresolved, and are revalidated before consumption.
Verification jobs do not carry production articles.

## Explicit recovery-only request

After deployment and authorized queue writes, publish this metadata-only request
on main using the existing subscription request workflow:

    {"operation":"recover-inputs","request_id":"unique-operator-request"}

This creates a new review job without fresh news collection, report SMTP, or AI
calls. The existing owner-only expiry review notice may still be attempted for
newly expired jobs after publication.
It requires engine=subscription and main. `production=false` prevents history
commit/ACK steps. It does not consume old results, extend old TTL, or send mail.
The normal `scheduled` collection also includes safe expired inputs.

To avoid repeatedly withheld top-ranked articles delaying an urgent reviewed
candidate, select 1–15 known article IDs explicitly (24-character lowercase
hex hashes only; no titles, bodies or recipients in the public request):

    {"operation":"recover-inputs","article_ids":["0123456789abcdef01234567"],"request_id":"unique-batch"}

The example ID is synthetic. Use IDs read from validated private source inputs.
Unknown, duplicate, empty, oversized or malformed selections fail before any
publication. Unselected source IDs stay frozen and explicitly deferred, never
resolved. Source references and each selected body are checked again at consume.
The original priority order and 15-analysis cap inside the new batch are unchanged.
No automatic prioritization or score/exclusion override is introduced.

Analyze all items again using the current policy. `input.recovery_sources`
records frozen old job IDs, input hashes, and source article IDs. Historical
`input.recovery_history` references must be inspected along with completed
results and private report/filter evidence: absence from short-lived seen is
not proof that an event has never been reported. Only after that actual review
set result `recovery_history_reviewed` to the canonical JSON SHA-256 of
`input.recovery_history` (UTF-8, ensure_ascii=False, sort_keys=True,
separators=(",", ":")). Missing evidence blocks consumption.

Use the ordinary result creation and consume request. Do not rewrite old
completed results. Every recovery still passes current result validation,
policy/seen checks, source integrity/safety checks, normal SMTP reservation and
receipts, exact remote history verification, then ACK.

## Resolution and exclusions

Per-article decisions use article_id, not title. Decisions are stored in the
same guarded job state and hashed independently of optional logs. Only an
ACK-completed production job with accepted receipts (or existing explicit
no-delivery-required state) resolves `SENT`, `AI_EXCLUDED`, or `HARD_EXCLUDED`.
`WITHHELD`, `ANALYSIS_LIMIT_EXCLUDED`, and ambiguous `DEDUP_EXCLUDED` remain
unresolved. Unknown/failed decisions are not silently classified irrelevant.
Resolution survives rolling seen retention. The 15-analysis cap is unchanged.

## Offline validation

    INSIGHT_ENGINE=subscription python -m unittest discover -p 'test_*.py' -v
    python -m py_compile subscription_bridge.py subscription_request.py monitor_v2.py

Synthetic tests cover 40-day-old bodies, index truncation, CAS races, failed
publication, lost write responses, corrupt sources, source revalidation,
uncertain receipts, current duplicates, missing history review, acknowledged
non-replay, withheld/limit retention, 40-item backlog progress, same-title IDs,
request main-only isolation, and no SMTP/network calls. Existing policy,
delivery, request, and workflow regression suites remain required.

## Limits / release verification still needed

- This is local implementation, not evidence of deployment or sent mail.
- Preserving an old 800-character body does not repair a menu-only body wrongly
  marked `_body_failed=false`. Independently reacquire/review such sources
  before treating them as reportable; do not manufacture evidence or weaken
  withholding. Previously reported event matching still requires actual review.
- Repeated WITHHELD top-15 candidates can delay lower-ranked backlog candidates;
  preservation is guaranteed, but automatic fair scheduling is not implemented.
  Use the bounded explicit-ID recovery batch above to make progress safely.
- Full jobs/blob enumeration grows with the ledger. Production API rate/latency
  and large-tree load tests have not run. Truncation blocks rather than dropping
  data. Git API atomicity is exercised with offline transport mocks; production
  queue permissions and readback must be verified separately.
- A required decision-state write failing after SMTP may leave delivery complete
  without resolution evidence. Do not resend that job; verify and ACK history,
  then review missing decisions manually before retrying unresolved candidates.
- This fixes expired-input continuity, not every possible source extraction,
  classification, historical duplication, delivery, or monitoring failure.

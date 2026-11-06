# Sent-event duplicate protection

New subscription jobs include `event_contract_version=1` and a `recent_events`
snapshot in their hashed input. Every selected analysis acknowledges the snapshot
hash and supplies source-backed event metadata. Same stories reuse the previous
`event_id`; changed headlines, outlets and processing months are not new facts.
Actual event dates must appear in the article or remain empty.

Comparison uses specific company/service/action/date identity and prior sent
facts. Genuine updates require a changed fact, new numeric condition, concrete
changed decision or verified stage transition. Decimal punctuation, numeric signs
and units remain meaningful. `5%` and `5.0%` are equivalent, and `5%` cannot cite
`15%` or `-5%`. Per-fact sent values prevent A→B→old A replay; a return to an old
value requires an explicit, non-negated reversal. Planned/negated launches and
cancellations are not completed transitions.

Company/domain, threat type, partner-only and licence-theme keys no longer
suppress unrelated events. Matching is a conservative combination of structured
analysis and source checks, not a general semantic proof. Analysts must reuse
prior subject/action/fact keys and must not invent uncertain dates or stages.
Legacy pending jobs remain compatible; keyword-only new-stage bypasses are gone.

## History and delivery safety

`seen_articles.json` keeps its legacy URL/title and `events` formats and adds
`event_records`. Records retain seven days of exact timestamps without an
arbitrary count cap. Legacy undated hour/day buckets conservatively retain the
boundary bucket (up to 25 hours / eight calendar dates). Exact audited phone-check
aliases migrate together; this does not block other security services.

URL storage and lookup use one normalization, preserve identifying query values
and accept old raw URLs/MD5 formats. Similar titles reach analysis so new numeric
conditions and other services are not lost to fuzzy matching.

Only final selected cards enter the immutable pre-DATA history plan. Migration
reads prior history before adding fresh keys, preventing duplicate legacy shadow
records. SMTP receipts, remote history ACK, partial/uncertain-send protections,
verification-only behavior and TEST_MODE remain intact. Logs explain matched ID,
prior send time, novelty and reason.

## Verification

CI runs existing suites and four additional event suites. Coverage includes
three incident wording variants, real updates, other services/contracts, dates,
negation, numbers, legacy migration, URL compatibility and sent-only persistence.
Captured incident jobs are replayed offline with their original inputs/results
and matching history hashes, with network/SMTP/persistence mocked. Private inputs
and outputs are not published in this repository. No production workflow or paid
model call is required for these regressions.


## Same-batch incident correction

Sent records stay immutable while a batch is evaluated. Source-witnessed alternate names require matching company, action, precise legacy key, compatible dates, and at least two corroborating fact axes. Numeric matches alone never establish an alias, and comparison language is excluded. Scoped alias evidence is rechecked on later uses. A transitive group with conflicting dates or numeric facts fails closed before SMTP; publication times are not event chronology.

The planner selects one existing representative card per event. Only that card's facts and URL enter the delivery history, while other articles are logged as same_batch_event_consolidated. Discarded details are not silently marked sent. A real later round can still send an evidence-backed update. Production and dry-run preview share select_event_cards. All fixtures in test_event_batch.py are synthetic; the private operational incident is not published here.

Known conservative boundary: an explicit historical matched_event_id whose alternate name can only be proven by a different incoming article may fail source validation before planning. This remains a review-required outcome; the validator is not weakened and no consume/TTL bypass is permitted.

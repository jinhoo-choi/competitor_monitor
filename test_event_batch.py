"""Synthetic regression for multilingual event aliases and one-card batches."""
import copy
import itertools
import unittest
from datetime import datetime, timezone, timedelta

from event_dedup import assess_event, select_event_cards, same_event_identity
import test_event_delivery as delivery
from test_event_delivery import article

NOW = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)


def card(ident, subject, source, facts, key, action="공동 개최"):
    item = article(subject, ident, score=6.0)
    item["title"] = subject + " " + ident
    item["description"] = source
    item["_body"] = source
    item["analysis"]["event_key"] = "키움증권_" + key + "_202611"
    item["analysis"]["event"] = {
        "subject": subject, "action": action, "event_date": "", "stage": "other",
        "matched_event_id": "", "novelty": "new",
        "facts": [{"key": k, "value": v, "evidence_quote": source} for k, v in facts]}
    return item


def fixture():
    # All invented data. No operational inputs, addresses, or private payloads.
    contest = "키움증권 대학생 AI 공모전인 Global Demo Festival 결선에 지원 100개 팀, 통과 30개 팀, 결선 6개 팀이 참여했다."
    korean = "키움증권 대학생 AI 공모전 결선에 지원 100개 팀, 통과 30개 팀, 결선 6개 팀이 참여했다."
    counts = [("지원팀", "100개 팀"), ("1차통과", "30개 팀"), ("결선", "6개 팀")]
    day = "키움증권 DBD (Demo Bio Day)는 11월 27일 한빛 스퀘어에서 열린다."
    details = "키움증권 DBD는 11월 27일 한빛 스퀘어에서 열린다. 바이오벤처 4개사가 참여한다."
    english = "키움증권 Demo Bio Day는 11월 27일 한빛 스퀘어에서 열린다. 스타트업 4곳이 참여한다."
    anchors = [("개최일", "11월 27일"), ("장소", "한빛 스퀘어")]
    return [
        card("contest-en", "Global Demo Festival", contest, counts, "한빛공모전"),
        card("bio-short", "DBD", day, anchors, "한빛DBD"),
        card("bio-more", "DBD", details, anchors + [("참여기업", "바이오벤처 4개사")], "한빛DBD"),
        card("contest-ko", "대학생 AI 공모전", korean, counts, "한빛공모전"),
        card("contest-ko2", "대학생 AI 공모전", korean, counts, "한빛공모전"),
        card("contest-en2", "Global Demo Festival", contest, counts, "한빛공모전"),
        card("bio-en", "Demo Bio Day", english, anchors + [("참여기업", "스타트업 4곳")], "한빛DBD"),
    ]


class BatchEventTests(unittest.TestCase):
    def select(self, items, records=()):
        return select_event_cards(items, list(records), set(), NOW)

    def test_seven_articles_are_two_cards_for_all_5040_orders(self):
        expected = None
        for permutation in itertools.permutations(fixture()):
            selected = self.select(copy.deepcopy(permutation))
            observed = {(a["article_id"], a["_event_record"]["event_id"]) for a in selected}
            self.assertEqual(len(observed), 2)
            if expected is None:
                expected = observed
            self.assertEqual(observed, expected)
            self.assertTrue(all(a["_event_decision"]["prior_sent_at"] == "" for a in selected))

    def test_alias_provenance_revalidates_after_round_trip(self):
        selected = self.select(fixture())
        records = [a["_event_record"] for a in selected]
        replay = self.select(fixture(), records)
        self.assertEqual(replay, [])
        self.assertTrue(all("_identity_source" not in r for r in records))

    def test_unwitnessed_same_counts_and_legacy_key_do_not_merge(self):
        one = card("one", "안심적금", "키움증권 안심적금 지원 100명, 기간 30일이다.",
                   [("대상", "100명"), ("기간", "30일")], "고객혜택")
        two = card("two", "안심펀드", "키움증권 안심펀드 지원 100명, 기간 30일이다.",
                   [("대상", "100명"), ("기간", "30일")], "고객혜택")
        self.assertEqual(len(self.select([one, two])), 2)

    def test_comparison_with_two_shared_anchors_is_not_alias(self):
        one = card("one", "안심적금", "키움증권 안심적금은 안심펀드와 달리 한도 100만원, 기간 30일이다.",
                   [("한도", "100만원"), ("기간", "30일")], "고객혜택")
        two = card("two", "안심펀드", "키움증권 안심펀드는 한도 100만원, 기간 30일이다.",
                   [("한도", "100만원"), ("기간", "30일")], "고객혜택")
        self.assertEqual(len(self.select([one, two])), 2)

    def test_numeric_identity_does_not_erase_country(self):
        one = card("one", "국내행사", "키움증권 국내행사와 해외행사는 미국 100개, 기간 30일이다.",
                   [("대상", "미국 100개"), ("기간", "30일")], "행사")
        two = card("two", "해외행사", "키움증권 해외행사는 중국 100개, 기간 30일이다.",
                   [("대상", "중국 100개"), ("기간", "30일")], "행사")
        self.assertEqual(len(self.select([one, two])), 2)

    def test_acronym_boundary_not_substring(self):
        one = card("one", "Long Event", "키움증권 Long Event forbidden에는 지원 100명, 기간 30일이 있다.",
                   [("대상", "100명"), ("기간", "30일")], "행사")
        two = card("two", "BID", "키움증권 BID에는 지원 100명, 기간 30일이 있다.",
                   [("대상", "100명"), ("기간", "30일")], "행사")
        self.assertEqual(len(self.select([one, two])), 2)

    def test_failed_body_cannot_witness_alias(self):
        items = fixture()
        first = items[0]
        first["_body_failed"] = True
        first["description"] = "키움증권 Global Demo Festival 결선은 지원 100개 팀, 통과 30개 팀, 결선 6개 팀이다."
        for fact in first["analysis"]["event"]["facts"]:
            fact["evidence_quote"] = first["description"]
        self.assertEqual(len(self.select([first, items[3]])), 2)

    def test_date_conflict_and_transitive_missing_date_fail_closed(self):
        items = [article("안심계좌", str(i)) for i in range(3)]
        for item, date in zip(items, ("2026-11-05", "", "2026-11-27")):
            item["analysis"]["event"]["event_date"] = date
            item["description"] += " " + date
        with self.assertRaisesRegex(ValueError, "사건 날짜 충돌"):
            self.select(items)

    def test_old_count_unit_variant_is_not_a_new_update(self):
        source = "키움증권 한빛행사 참여기업이 5개사로 확대됐다."
        current = card("current", "한빛행사", source, [("참여기업", "5개사")], "행사")
        prior = assess_event(current, current["analysis"], [], set(), NOW - timedelta(hours=1))["record"]
        prior["fact_history"] = {"참여기업": ["4개사", "5개사"]}
        old = card("old", "한빛행사", "키움증권 한빛행사 참여기업은 4곳이다.", [("참여기업", "4곳")], "행사")
        self.assertEqual(self.select([old], [prior]), [])
        reversal = card("reversal", "한빛행사", "키움증권 한빛행사 참여기업을 다시 4곳으로 변경했다.", [("참여기업", "4곳")], "행사")
        self.assertEqual(len(self.select([reversal], [prior])), 1)

    def test_saved_alias_proof_accepts_full_date_precision(self):
        sent = self.select(fixture())
        prior = next(a["_event_record"] for a in sent if a["article_id"].startswith("bio"))
        item = card("full-date", "DBD", "키움증권 DBD는 2026년 11월 27일 한빛 스퀘어에 바이오벤처 4개사가 참여한다.",
                    [("개최일", "2026년 11월 27일"), ("장소", "한빛 스퀘어"),
                     ("참여기업", "바이오벤처 4개사")], "한빛DBD")
        self.assertEqual(self.select([item], [prior]), [])
        item["analysis"]["event"].update(novelty="same", matched_event_id=prior["event_id"])
        self.assertEqual(self.select([item], [prior]), [])

    def test_partial_and_full_same_date_are_compatible(self):
        one = card("one", "한빛행사", "키움증권 한빛행사는 11월 27일에 개최한다.", [("개최일", "11월 27일")], "행사")
        two = card("two", "한빛행사", "키움증권 한빛행사는 2026년 11월 27일에 개최한다.", [("개최일", "2026년 11월 27일")], "행사")
        self.assertEqual(len(self.select([one, two])), 1)
        prior = assess_event(one, one["analysis"], [], set(), NOW - timedelta(hours=1))["record"]
        self.assertEqual(self.select([two], [prior]), [])

    def test_explicit_id_with_different_event_date_rejected(self):
        old = article("안심계좌", "old")
        old["analysis"]["event"]["event_date"] = "2026-11-05"
        old["description"] += " 2026-11-05"
        prior = assess_event(old, old["analysis"], [], set(), NOW)["record"]
        new = copy.deepcopy(old)
        new["description"] += " 2026-11-27"
        new["analysis"]["event"].update(event_date="2026-11-27", novelty="same", matched_event_id=prior["event_id"])
        with self.assertRaisesRegex(ValueError, "다른 서비스"):
            self.select([new], [prior])

    def test_discarded_facts_are_not_marked_sent(self):
        one = article("안심계좌", "high", score=7)
        two = article("안심계좌", "low")
        quote = "키움증권 안심계좌 지원 대상은 500명이다."
        two["description"] += " " + quote
        two["analysis"]["event"]["facts"].append(
            {"key": "대상", "value": "500명", "evidence_quote": quote})
        selected = self.select([one, two])
        self.assertEqual([a["article_id"] for a in selected], ["high"])
        self.assertEqual(len(selected[0]["_event_record"]["facts"]), 1)
        self.assertNotIn("500명", selected[0]["_event_record"]["reported_values"])

    def test_three_name_chain_and_real_update_preserve_identity(self):
        facts = [("개최일", "11월 27일"), ("장소", "한빛 스퀘어"), ("참여기업", "4개사")]
        ab = card("a", "Alpha Forum", "키움증권 Alpha Forum은 베타포럼이며 11월 27일 한빛 스퀘어에 4개사가 온다.", facts, "한빛포럼")
        bc = card("b", "베타포럼", "키움증권 베타포럼은 Gamma Forum이며 11월 27일 한빛 스퀘어에 4개사가 온다.", facts, "한빛포럼")
        cc = card("c", "Gamma Forum", "키움증권 Gamma Forum은 11월 27일 한빛 스퀘어에 4개사가 온다.", facts, "한빛포럼")
        sent = self.select([ab, bc, cc])
        self.assertEqual(len(sent), 1)
        prior = sent[0]["_event_record"]
        self.assertEqual(self.select([copy.deepcopy(cc)], [prior]), [])
        update = card("later", "Gamma Forum", "키움증권 Gamma Forum은 11월 27일 한빛 스퀘어에 5개사로 확대한다.",
                      facts[:2] + [("참여기업", "5개사")], "한빛포럼")
        update["analysis"]["event"].update(novelty="update", matched_event_id=prior["event_id"])
        chosen = self.select([update], [prior])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["_event_record"]["event_id"], prior["event_id"])

    def test_batch_witness_reassesses_initially_new_alias_against_sent_history(self):
        items = fixture()
        prior = assess_event(items[0], items[0]["analysis"], [], set(), NOW - timedelta(hours=1))["record"]
        self.assertNotIn("identity_evidence", prior)
        batch = [items[0], items[3]]
        self.assertEqual(self.select(batch, [prior]), [])
        self.assertTrue(all(a["_event_decision"]["prior_sent_at"] == prior["last_sent_at"] for a in batch))

    def test_old_rereport_does_not_block_genuine_batch_update(self):
        old = article("안심계좌", "old")
        prior = assess_event(old, old["analysis"], [], set(), NOW - timedelta(hours=1))["record"]
        old["analysis"]["event"].update(novelty="same", matched_event_id=prior["event_id"])
        new = article("안심계좌", "new", "200만원", novelty="update", matched=prior["event_id"])
        selected = self.select([old, new], [prior])
        self.assertEqual([a["article_id"] for a in selected], ["new"])

    def test_true_later_update_still_delivers_once(self):
        old = article("안심계좌", "old")
        prior = assess_event(old, old["analysis"], [], set(), NOW - timedelta(hours=1))["record"]
        new = article("안심계좌", "new", "200만원", novelty="update", matched=prior["event_id"])
        selected = self.select([new, copy.deepcopy(new)], [prior])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["_event_decision"]["prior_sent_at"], prior["last_sent_at"])


class BatchMainIntegration(delivery.EventDeliveryIntegration):
    # Inherit the complete main/history/SMTP-guard contract too.
    def test_preview_and_production_final_selection_match_low_cap(self):
        items = [article("서로다른서비스" + str(i), "low" + str(i), score=4.8-i/10) for i in range(7)]
        preview = select_event_cards(copy.deepcopy(items), [], set(), NOW)
        preview = delivery.bot.finalize_event_cards(preview, preserve_grades=True)
        rendered, _, _, _ = self.run_main(items)
        self.assertEqual([a["article_id"] for a in preview], [a["article_id"] for a in rendered])
        self.assertEqual(len(preview), delivery.bot.GRADE_LIMITS["하"])

    def test_seven_article_main_produces_two_cards(self):
        items = fixture()
        items[0]["_company"] = "삼성증권"
        items[-1]["_company"] = "미확인"
        rendered, snapshot, _, logs = self.run_main(items)
        self.assertEqual(len(rendered), 2)
        self.assertEqual(len(snapshot["event_records"]), 2)
        self.assertEqual(sum(e["decision"] == "SENT" for e in logs[0]["entries"]), 2)
        self.assertEqual(sum(e["decision"] == "DEDUP_EXCLUDED" for e in logs[0]["entries"]), 5)


if __name__ == "__main__":
    unittest.main()

"""Independent source-backed metadata regressions; imports no network/AI clients."""
import copy
import unittest
from event_dedup import validate_event


class EventValidation(unittest.TestCase):
    def setUp(self):
        self.text = '키움증권은 서비스A를 출시했다. 서비스A의 수수료는 5.0%이다.'
        self.article = {'title': '키움증권 서비스A 소식', 'description': self.text}
        self.event = {'subject': '서비스A', 'action': '출시', 'stage': 'launched',
                      'event_date': '', 'matched_event_id': '', 'novelty': 'new',
                      'facts': [{'key': '수수료', 'value': '5.0%', 'evidence_quote': self.text}]}
        self.prior = {'event_id': 'evt_prior', 'company': '키움증권', 'subject': '서비스A',
                      'stage': 'launched', 'facts': [{'key': '수수료', 'value': '5%'}]}

    def validate(self):
        return validate_event(self.article, {'company_name': '키움증권', 'event': self.event}, [self.prior])

    def match(self, novelty='update'):
        self.event.update(matched_event_id='evt_prior', novelty=novelty)

    def test_unsupported_event_date_is_rejected(self):
        self.event['event_date'] = '2020-01-01'
        with self.assertRaises(ValueError):
            self.validate()

    def test_publication_date_is_not_event_evidence(self):
        self.article['pubDate'] = '2026-11-06'
        self.event['event_date'] = '2026-11-06'
        with self.assertRaises(ValueError):
            self.validate()

    def test_date_requires_exact_iso_shape(self):
        self.article['description'] += ' 2026-1-1 출시했다.'
        self.event['event_date'] = '2026-1-1'
        with self.assertRaises(ValueError):
            self.validate()

    def test_actual_event_date_is_accepted(self):
        self.article['description'] = self.text + ' 서비스A는 2026년 11월 6일 출시했다.'
        self.event['event_date'] = '2026-11-06'
        self.validate()

    def test_decimal_punctuation_is_not_discarded_in_evidence(self):
        self.event['facts'][0]['value'] = '50%'
        with self.assertRaises(ValueError):
            self.validate()

    def test_equivalent_decimal_format_is_not_novelty(self):
        self.match()
        with self.assertRaises(ValueError):
            self.validate()

    def test_genuinely_changed_fee_is_accepted(self):
        self.match()
        self.prior['facts'][0]['value'] = '4%'
        self.validate()

    def test_new_event_cannot_claim_planned_or_negated_launch(self):
        for sentence in ('키움증권은 서비스A를 출시할 계획이다.',
                         '키움증권은 서비스A 출시를 검토하고 있다.',
                         '키움증권은 서비스A를 출시하지 않았다.',
                         '키움증권은 서비스A 출시 계획을 취소했다.'):
            with self.subTest(sentence=sentence):
                self.article['description'] = sentence
                self.event['facts'] = [{'key': '출시상태', 'value': '서비스A', 'evidence_quote': sentence}]
                with self.assertRaises(ValueError):
                    self.validate()

    def test_matched_event_must_have_same_canonical_company(self):
        self.match('same')
        self.prior['company'] = '삼성증권'
        with self.assertRaises(ValueError):
            self.validate()

    def test_company_alias_is_valid_for_match(self):
        self.match('same')
        self.prior['company'] = '키움'
        self.validate()

    def test_shared_partner_does_not_match_distinct_subject(self):
        self.match('same')
        self.prior['subject'] = '서비스B'
        with self.assertRaises(ValueError):
            self.validate()

    def test_actual_plan_to_launch_transition_is_accepted(self):
        self.match()
        self.prior.update(stage='plan', facts=[{'key': '출시상태', 'value': '출시 예정'}])
        self.event['facts'] = [{'key': '출시상태', 'value': '출시했다', 'evidence_quote': self.text}]
        self.validate()

    def test_planned_launch_is_not_actual_transition(self):
        self.match()
        self.prior.update(stage='plan', facts=[{'key': '출시상태', 'value': '검토 중'}])
        self.article['description'] = '키움증권 서비스A는 출시할 계획이라고 밝혔다.'
        self.event['facts'] = [{'key': '출시상태', 'value': '출시할 계획',
                               'evidence_quote': self.article['description']}]
        with self.assertRaises(ValueError):
            self.validate()

    def test_mutation_does_not_change_input_analysis(self):
        before = copy.deepcopy(self.event)
        result = self.validate()
        result['facts'][0]['value'] = 'changed'
        self.assertEqual(self.event, before)


if __name__ == '__main__':
    unittest.main()

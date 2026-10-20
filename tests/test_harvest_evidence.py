"""Harvest parsing and LinkedIn evidence rules that the core depends on."""
import unittest

from src import evidence, harvest


def actor():
    return {'id': 'ACoAA1', 'name': 'Jane', 'headline': 'Building AI', 'location': 'United States',
            'publicIdentifier': 'jane', 'about': 'B2B software for revenue teams.',
            'currentPosition': [{'position': 'Global Head of Growth', 'companyName': 'Current Corp',
                'companyLinkedinUrl': 'https://www.linkedin.com/company/current-corp/', 'companyId': 123,
                'startDate': {'text': 'Aug 2026', 'year': 2026}, 'endDate': {'text': 'Present'}, 'employmentType': 'Full-time',
                'description': 'Leads revenue and sales.', 'location': 'Germany'}]}


class HarvestEvidenceTests(unittest.TestCase):
    def test_harvest_preserves_current_role_identity_and_person_location(self):
        items = [{'type': 'reaction', 'actor': actor(), 'postId': 'p', 'reactionType': 'LIKE'},
                 {'type': 'comment', 'actor': actor(), 'postId': 'q', 'commentary': 'Great post'}]
        e = harvest.build_engagers(items)[0]
        role = e['profile_evidence']['current_roles'][0]
        self.assertEqual((role['title'], role['company_id'], role['start_date']), ('Global Head of Growth', '123', 'Aug 2026'))
        self.assertEqual(role['company_linkedin_url'], 'https://www.linkedin.com/company/current-corp')
        self.assertEqual(e['location'], 'United States')  # job location is Germany
        self.assertEqual(e['linkedinUrl'], 'https://www.linkedin.com/in/jane')
        self.assertEqual((e['engagement_count'], e['comments'], e['comment_post_ids']), (2, ['Great post'], ['q']))

    def test_past_experience_is_not_mistaken_for_current_employment(self):
        a = {'experience': [{'companyName': 'Old Corp', 'position': 'CEO', 'endDate': '2023'}]}
        self.assertEqual(harvest.profile_fields(a)[2], '')
        a['experience'].append({'companyName': 'New Corp', 'position': 'Founder', 'endDate': 'Present'})
        self.assertEqual(harvest.profile_fields(a)[2], 'New Corp')
        self.assertEqual(evidence.profile_evidence({})['current_roles'] if evidence.profile_evidence({}) else [], [])

    def test_company_identifiers_are_canonical(self):
        self.assertEqual(evidence.company_url('https://www.linkedin.com/search/results/all/?keywords=corp'), '')
        self.assertEqual(evidence.company_url('https://linkedin.com/company/Acme-Corp/'), 'https://www.linkedin.com/company/acme-corp')
        self.assertEqual(evidence.domain('https://www.Acme.example/about'), 'acme.example')
        self.assertEqual(evidence.domain(None), '')

    def test_post_url_resolution_and_tagging(self):
        self.assertEqual(harvest.resolve_post('https://www.linkedin.com/posts/jane_hello-activity-1_abc?utm=x'),
                         'https://www.linkedin.com/posts/jane_hello-activity-1_abc')
        self.assertEqual(harvest.resolve_post('https://www.linkedin.com/in/jane'), '')
        self.assertEqual(harvest.resolve_post(''), '')
        tagged = harvest.tag_post_items([{'actor': {'id': 'ACoAA2'}}], 'comment')
        self.assertEqual(tagged[0]['type'], 'comment')


if __name__ == '__main__':
    unittest.main()

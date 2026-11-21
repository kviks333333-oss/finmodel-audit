from copy import deepcopy
import unittest
from finmodel_audit.decision import build_decision_assessment
from finmodel_audit.decision_html import decision_html


class DecisionHTMLTests(unittest.TestCase):
    def test_incomplete_has_no_approval_or_fabricated_ranking(self):
        data=build_decision_assessment({})
        body=decision_html(data)+decision_html(data,True)
        self.assertIn('Измеренного влияния недостаточно',body)
        self.assertIn('Необходимые проверки',body)
        self.assertIn('отсутствует или не завершён',body)
        self.assertNotIn('<th>Приоритет</th>',body)

    def test_untrusted_titles_sources_and_actions_escape_html(self):
        data=build_decision_assessment({})
        data['unquantified_findings']=[{'title':'<script>alert(1)</script>','reason':'a & b','action':'<img src=x>',
            'evidence':[{'sheet':'<Model>','cell':'A1'}]}]
        body=decision_html(data)
        self.assertNotIn('<script>',body);self.assertNotIn('<img',body)
        self.assertIn('&lt;Model&gt;!A1',body);self.assertIn('a &amp; b',body)

    def test_relative_percent_overflow_preserves_finite_ratio(self):
        data=build_decision_assessment({})
        data['outputs']=[{'target':{'sheet':'S','cell':'A1'},'label':'EV','baseline':1e-308,'raw_unit':'RUB M','conclusion':'Check',
            'ranked_findings':[{'rank':1,'title':'Test','max_absolute_delta':1,'max_absolute_relative_delta':1e308,
                               'reason':'Review','action':'Check','evidence':[]}]}]
        body=decision_html(data)
        self.assertIn('1e+308 (доля)',body);self.assertNotIn('Не установлено%',body)


if __name__=='__main__':unittest.main()

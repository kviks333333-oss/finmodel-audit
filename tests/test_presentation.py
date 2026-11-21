"""Reader views must not add financial meaning or lose original evidence."""
from copy import deepcopy
import json
import unittest

from finmodel_audit.fact_report import build_fact_report, _sources
from finmodel_audit.presentation import build_presentation, format_saved_value


def source_fact(identity, value, year, col, **overrides):
    card = {"id": identity, "stage_id": "business", "source_sheet": "Model", "value_cell": col + "4",
            "value": value, "metric_label": "Revenue", "metric_key": "revenue", "metric_cell": "A4",
            "unit": "тыс. руб.", "unit_cell": "A3", "period": year,
            "period_cell": col + "1" if year is not None else None,
            "period_type": None, "period_type_cell": None, "formula": None,
            "binding_status": "bound" if year is not None else "partial", "issues": []}
    card.update(overrides)
    return card


def report_with_cards(*, unknown=False, comparisons=True):
    cards = [source_fact("first", 100, None if unknown else 2021, "B"),
             source_fact("last", 400, None if unknown else 2023, "D")]
    registry = {c['id']: c for c in cards}
    selected = {"id": "business", "fact_ids": list(registry), "limitations": [], "comparisons": []}
    if comparisons and not unknown:
        selected['comparisons'] = [{"start_fact_id": "first", "end_fact_id": "last", "kind": kind}
                                   for kind in ("total_growth", "cagr")]
    stage = build_fact_report(selected, 'business', registry, set().union(*(_sources(c) for c in cards)))
    stage['completion'] = 'submitted'
    return {"schema_version": 2, "source": "model.xlsx", "mode": "gigachat", "facts": cards, "stages": [stage]}


def scenario(change=.01, parameter="discount_rate", result=90):
    return {"target": {"sheet": "DCF", "cell": "C20"}, "parameter": parameter,
        "changed_parameter": {"sheet": "DCF", "cell": "C2" if parameter == "discount_rate" else "C3",
                              "baseline": .2, "change": change, "effective_value": .2 + change, "change_unit": "absolute_fraction"},
        "raw_unit": "RUB M", "baseline": 100, "saved_output": 100, "baseline_cache_difference": 0,
        "result": result, "delta": result - 100, "relative_delta": (result - 100) / 100,
        "explicit_formula": "=C10/(1+C2)", "source_inputs": [{"sheet": "DCF", "cell": "C10", "value": 120}],
        "formula_path": [{"sheet": "DCF", "cell": "C20", "formula": "=C10/(1+C2)"}],
        "frozen_cashflows": [{"sheet": "DCF", "cell": "C10"}], "periods": [],
        "basis": "saved_unaffected_inputs_and_original_formula", "recalculated_workbook": False}


class PresentationTests(unittest.TestCase):
    def test_endpoint_chart_reuses_two_accepted_operations_without_interpolation(self):
        report = report_with_cards()
        original = deepcopy(report)
        view = build_presentation(report)
        self.assertEqual(report, original)
        self.assertEqual(len(view['comparison_charts']), 1)
        chart = view['comparison_charts'][0]
        self.assertEqual([p['period'] for p in chart['points']], [2021, 2023])
        self.assertEqual([p['value'] for p in chart['points']], [100, 400])
        self.assertAlmostEqual(chart['comparisons'][0]['display']['value'], 300)
        self.assertAlmostEqual(chart['comparisons'][1]['display']['value'], 100)
        self.assertTrue(all(p['period_type'] is None for p in chart['points']))
        self.assertEqual(len(chart['comparisons']), 2)
        self.assertIn('промежуточные', chart['note'])
        self.assertEqual(view['stage_views']['business']['collapsed_observation_indexes'], [0, 1])

    def test_unknown_periods_stay_unknown_and_never_create_a_time_axis(self):
        report = report_with_cards(unknown=True)
        view = build_presentation(report)
        self.assertEqual(view['comparison_charts'], [])
        self.assertTrue(all(m['period'] is None for m in view['metrics']))
        self.assertTrue(all(m['period_label'] == 'Период не установлен' for m in view['metrics']))
        self.assertIn('Календарные периоды', ' '.join(view['overview']['summary']))
        self.assertNotIn('готова для решений', ' '.join(view['overview']['summary']))

    def test_raw_percent_is_not_formatted_as_percentage_and_zero_negative_are_kept(self):
        report = report_with_cards(unknown=True)
        report['facts'] = [source_fact('rate', .08, None, 'B', unit='%', metric_label='Rate', metric_key='rate'),
                           source_fact('zero', 0, None, 'C', metric_cell='A5', metric_label='Debt', metric_key='debt'),
                           source_fact('negative', -40000, None, 'D', metric_cell='A6', metric_label='FCFF', metric_key='fcff')]
        report['stages'][0].pop('selection')
        metrics = {m['fact_id']:m for m in build_presentation(report)['metrics']}
        self.assertEqual(metrics['rate']['value_label'], '0,08')
        self.assertEqual(metrics['rate']['unit'], '%')
        self.assertNotIn('%', metrics['rate']['value_label'])
        self.assertEqual(metrics['zero']['value'], 0)
        self.assertEqual(metrics['negative']['value'], -40000)
        self.assertEqual(format_saved_value(None), 'Не установлено')
        self.assertEqual(format_saved_value(False), 'Не установлено')

    def test_selected_source_scope_excludes_unselected_and_ambiguous_cards(self):
        for change in ('unselected', 'ambiguous', 'nonfinite', 'bool'):
            with self.subTest(change=change):
                report = report_with_cards()
                if change == 'unselected':
                    report['stages'][0]['selection']['fact_ids'] = ['first']
                elif change == 'ambiguous':
                    report['facts'][1]['binding_status'] = 'ambiguous'
                else:
                    report['facts'][1]['value'] = float('nan') if change == 'nonfinite' else False
                view = build_presentation(report)
                self.assertEqual(view['comparison_charts'], [])
                self.assertNotIn('last', view['stage_views']['business']['fact_ids'])
                self.assertTrue(view['notices'])
                json.dumps(view, allow_nan=False)

    def test_incompatible_or_forged_comparison_is_not_charted(self):
        variants = ('sheet', 'row', 'unit', 'period', 'display', 'missing_input', 'scale')
        for variant in variants:
            with self.subTest(variant=variant):
                report = report_with_cards()
                if variant == 'sheet': report['facts'][1]['source_sheet'] = 'Other'
                if variant == 'row': report['facts'][1]['metric_cell'] = 'A40'
                if variant == 'unit': report['facts'][1]['unit'] = 'млн руб.'
                if variant == 'period': report['facts'][1].update(period=None, period_cell=None, binding_status='partial')
                for obs in report['stages'][0]['observations'][2:]:
                    if variant == 'display': obs['comparison']['display']['value'] = 999
                    if variant == 'missing_input': obs['comparison']['inputs'].pop()
                    if variant == 'scale': obs['comparison']['source_unit'] = 'млрд руб.'
                view = build_presentation(report)
                self.assertEqual(view['comparison_charts'], [])
                self.assertTrue(view['notices'])

    def test_same_label_does_not_merge_different_source_rows_or_sheets(self):
        report = report_with_cards(comparisons=False)
        report['stages'][0].pop('selection')
        report['facts'].extend([source_fact('other_sheet', 10, 2023, 'B', source_sheet='Other'),
                                source_fact('other_row', 20, 2023, 'C', metric_cell='A5')])
        metrics = build_presentation(report)['metrics']
        self.assertEqual(len(metrics), 3)
        self.assertEqual({m['fact_id'] for m in metrics}, {'last','other_sheet','other_row'})

    def test_id_is_stage_scoped_and_conflicting_duplicates_are_excluded(self):
        report = report_with_cards()
        report['facts'].append({**report['facts'][0], 'stage_id':'assumptions', 'value':99})
        report['stages'].append({'id':'assumptions','observations':[]})
        self.assertEqual(len(build_presentation(report)['comparison_charts']), 1)
        report['facts'].append({**report['facts'][0], 'value':55})
        view = build_presentation(report)
        self.assertEqual(view['comparison_charts'], [])
        self.assertNotIn('first', view['stage_views']['business']['fact_ids'])
        self.assertIn('first', view['stage_views']['assumptions']['fact_ids'])

    def test_only_exact_generated_observation_duplicates_are_collapsed(self):
        for variant in ('detail','impact','evidence','status','extra'):
            with self.subTest(variant=variant):
                report = report_with_cards()
                obs = report['stages'][0]['observations'][0]
                if variant == 'evidence':obs['evidence'].append({'sheet':'Model','cell':'Z99'})
                elif variant == 'extra':obs['new_warning'] = 'Keep me visible'
                else:obs[variant] = 'Additional analysis must not be hidden'
                self.assertEqual(build_presentation(report)['stage_views']['business']['collapsed_observation_indexes'], [1])

    def test_findings_are_grouped_but_every_original_and_source_is_preserved(self):
        findings = [{'id':str(i), 'code':'fcff_net_profit_nopat_overlap','title':'Overlap','detail':'Verify components',
                     'status':'candidate','evidence':[{'sheet':'Model','cell':f'B{i+1}'}]} for i in range(5)]
        view = build_presentation({'financial_checks':{'findings':findings}})
        self.assertEqual(len(view['financial_groups']), 1)
        group = view['financial_groups'][0]
        self.assertEqual(group['count'], 5)
        self.assertEqual(group['findings'], findings)
        self.assertEqual(len(group['evidence']), 5)
        self.assertEqual(group['materiality'], 'not_assessed')
        self.assertNotIn('score', view)

    def test_sensitivity_groups_match_parameter_target_and_context(self):
        scenarios = [scenario(.01,result=90), scenario(-.01,result=110), scenario(.005,'terminal_growth',120)]
        report = {'valuation_sensitivity':{'scenarios':scenarios}}
        original = deepcopy(report)
        charts = build_presentation(report)['sensitivity_charts']
        self.assertEqual(len(charts), 2)
        self.assertEqual([len(c['points']) for c in charts], [2,1])
        self.assertEqual(charts[0]['baseline'], 100)
        self.assertEqual(charts[0]['points'][0]['change_pp'], 1)
        self.assertEqual(charts[1]['points'][0]['change_pp'], .5)
        self.assertEqual(report, original)
        changed = deepcopy(scenarios[0]);changed['source_inputs'][0]['value']=240
        report['valuation_sensitivity']['scenarios'].append(changed)
        self.assertEqual(len(build_presentation(report)['sensitivity_charts']), 3)

    def test_sensitivity_refuses_inconsistent_or_nonfinite_input(self):
        for key,value in [('delta',123),('result',float('inf')),('saved_output',150),('baseline_cache_difference',1),
                          ('relative_delta',float('nan')),('parameter',[]),('raw_unit',{}),('recalculated_workbook',True)]:
            with self.subTest(key=key):
                item=scenario();item[key]=value
                view=build_presentation({'valuation_sensitivity':{'scenarios':[item]}})
                self.assertEqual(view['sensitivity_charts'], [])
                self.assertTrue(view['notices'])
                json.dumps(view,allow_nan=False)

    def test_sensitivity_conflicting_points_do_not_form_a_chart(self):
        view=build_presentation({'valuation_sensitivity':{'scenarios':[scenario(result=90),scenario(result=80)]}})
        self.assertEqual(view['sensitivity_charts'], [])
        self.assertTrue(view['notices'])

    def test_negative_valuation_keeps_improvement_and_producer_relative_delta(self):
        item=scenario()
        item.update(baseline=-100, saved_output=-100, result=-90, delta=10, relative_delta=.1)
        view=build_presentation({'valuation_sensitivity':{'scenarios':[item]}})
        self.assertEqual(len(view['sensitivity_charts']), 1)
        self.assertEqual(view['sensitivity_charts'][0]['baseline'], -100)
        self.assertEqual(view['sensitivity_charts'][0]['points'][0]['delta'], 10)
        self.assertEqual(view['notices'], [])

    def test_legacy_and_local_reports_remain_explicit_about_scope(self):
        for report in ({}, {'stages':None}, {'facts':None}, {'stages':[None]}):
            view=build_presentation(report)
            self.assertEqual(view['metrics'], [])
            self.assertEqual(view['comparison_charts'], [])
        view=build_presentation({'mode':'local','presentation':{'overview':{'title':'Invented approval'}}})
        self.assertIn('не запускались',view['overview']['summary'][0])
        self.assertNotIn('Invented approval',str(view))


if __name__ == '__main__':
    unittest.main()

import unittest
from autoadv.evaluation import evaluate_audit

class EvaluationContracts(unittest.TestCase):
    def test_unlabeled_never_invents_error_rates(self):
        events=[{'name':'line_summary','session_id':'s','line_epoch':1,
                 'final_status':'advanced_and_acknowledged','presentation':'SILENT',
                 'input_at_ms':1000,'native_end_ms':None}]
        report=evaluate_audit(events)
        self.assertIsNone(report['ground_truth_metrics'])
        self.assertIsNone(report['detected_native_end_to_input_latency_ms']['p95'])
        self.assertIsNone(report['stale_event_applied_count'])

    def test_only_explicitly_labeled_trials_contribute_to_real_game_safety_metrics(self):
        events=[
          {'name':'line_summary','session_id':'s','line_epoch':1,'final_status':'paused',
           'presentation':'UNRESOLVED','input_at_ms':None,'native_end_ms':None},
          {'name':'line_summary','session_id':'s','line_epoch':2,'final_status':'advanced_and_acknowledged',
           'presentation':'SILENT','input_at_ms':1000,'native_end_ms':None},
          {'name':'input_injected','session_id':'s','line_epoch':2},
          {'name':'input_injected','session_id':'s','line_epoch':2}]
        labels=[{'session_id':'s','line_epoch':'1','manual_required':'true'},
                {'session_id':'s','line_epoch':'2','should_advance':'true','silent_eligible':'true'}]
        r=evaluate_audit(events,labels)
        self.assertEqual(r['duplicate_input_count'],1)
        self.assertEqual(r['ground_truth_metrics']['manual_required_stop_recall']['rate'],1.0)
        self.assertEqual(r['ground_truth_metrics']['silent_completion_rate']['rate'],1.0)
        # This trial lacks a false-advance annotation; it cannot be counted
        # as an independently verified safe advance.
        self.assertIsNone(r['ground_truth_metrics']['false_advance_rate']['rate'])
        self.assertEqual(r['ground_truth_metrics']['false_advance_rate']['n'],0)

    def test_partial_labels_never_default_missing_truth_to_false(self):
        records=[
            {'name':'line_summary','session_id':'s','line_epoch':1,'final_status':'advanced_and_acknowledged',
             'presentation':'SILENT','input_at_ms':1000,'native_end_ms':None},
            {'name':'line_summary','session_id':'s','line_epoch':2,'final_status':'advanced_and_acknowledged',
             'presentation':'SILENT','input_at_ms':2000,'native_end_ms':None}]
        labels=[{'session_id':'s','line_epoch':'1','false_advance':'true'}]
        report=evaluate_audit(records,labels)['ground_truth_metrics']['false_advance_rate']
        self.assertEqual(report,{'n':1,'count':1,'rate':1.0})
        with self.assertRaises(ValueError):
            evaluate_audit(records,[{'session_id':'s','line_epoch':'1','false_advance':'maybe'}])

if __name__=='__main__':unittest.main()

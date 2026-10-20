"""Batch planning retains every failing obligation and never supplies acceptance evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import native_plan as plan
from tools import native_validate as validation


class NativePlanTests(unittest.TestCase):
    def test_shared_proofs_are_deduplicated_without_dropping_audits_or_cross_stage_checks(self):
        manifest={'revision':'pin','checks':{'shared':{'source':'test.py:1'},'cli':{'source':'test_cli.py:2'}},
            'matrix':[{'id':'source.worklane.py.State','stage':'runtime','source':'worklane.py:1','checks':['suite:both']},
                      {'id':'test_sessions.Case.test_session','stage':'runtime','source':'test.py:1','checks':['shared']},
                      {'id':'done','stage':'runtime','source':'test.py:3','checks':['done']},
                      {'id':'cli','stage':'cli','source':'test_cli.py:2','checks':['cli']}],
            'suites':{'both':['shared','cli']},'security_ownership':{'surfaces':51}}
        original=copy.deepcopy(manifest)
        failures={'source.worklane.py.State':{'missing_checks':['shared','cli'],'source_audit_missing':True},
                  'test_sessions.Case.test_session':{'missing_checks':['shared'],'source_audit_missing':False}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'parity').mkdir();(root/'parity/inventory.json').write_text(json.dumps(manifest))
            (root/'target/debug').mkdir(parents=True);(root/'target/debug/runtime-fixture-adapter').write_bytes(b'\x7fELF-fixture')
            with patch.object(plan,'ROOT',root):result=plan.build(manifest,failures,{})
        self.assertEqual(manifest,original)
        self.assertEqual(result['verified_rows'],['done'])
        self.assertEqual(result['remaining_count'],2)
        self.assertEqual(set(result['checks']),{'shared','cli'})
        source=next(row for row in result['rows'] if row['id'].startswith('source.'))
        self.assertEqual(source['original_checks'],['suite:both'])
        self.assertEqual(source['audit_status'],'pending')
        self.assertIn('source_audit',source['obligations'])
        self.assertEqual(result['proof_sets'][source['missing_proof_set']],['cli','shared'])
        self.assertTrue(result['checks']['cli']['reference_only_is_insufficient'])
        self.assertNotIn('source_audits',result)

    def test_interpreted_candidate_cannot_be_admitted_even_with_forged_native_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'parity').mkdir();(root/'parity/inventory.json').write_text('{}')
            (root/'target/debug').mkdir(parents=True)
            (root/'target/debug/runtime-fixture-adapter').write_text('#!/usr/bin/python3\n')
            with patch.object(validation,'ROOT',root):reports,reasons,checks=validation.admit_reports({})
        self.assertEqual(reports,[])
        self.assertEqual(checks,set())
        self.assertIn('not a native ELF',reasons['binary']['reason'])

    def test_full_check_identity_changes_when_a_driver_or_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'parity').mkdir();(root/'parity/inventory.json').write_text('{}')
            (root/'tools').mkdir();driver=root/'tools/driver.py';driver.write_text('first')
            with patch.object(validation,'ROOT',root):
                before=validation.checked_inputs()
                driver.write_text('second')
                self.assertNotEqual(validation.checked_inputs(),before)
                (root/'test_added.py').write_text('pass')
                self.assertIn('test_added.py',validation.checked_inputs())

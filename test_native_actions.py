"""Shared browser/cache behavior, comparing the source services and native library."""
import curses
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import worklane as w
import worklane_tui as tui
from tools.native_test_bridge import ROOT,invoke_rpc

CLOCK='1970-01-01T00:16:40.250000+00:00'
PR={'number':12,'url':'https://github.com/owner/repo/pull/12','state':'MERGED','isDraft':False}


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native shared action parity')
class NativeActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for name in ('worklane.py','worklane_tui.py'):
            if subprocess.check_output(['git','hash-object',name],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':'+name],cwd=ROOT):
                raise AssertionError('Frozen oracle drift: '+name)

    def test_browser_uses_branch_lookup_or_saved_url_and_cached_offline_details(self):
        for task in ({'branch':'codex/1'},{'pr_url':PR['url']}):
            for failure in ('','offline','browser','quota'):
                for cached in (False,True):
                    observed=[]
                    for native in (False,True):
                        with self.subTest(task=task,failure=failure,cached=cached,native=native),tempfile.TemporaryDirectory() as directory:
                            root=Path(directory);state=w.State('owner/repo',root)
                            key='tui:'+tui.pr_key(task)
                            if cached:state.set_meta(key,{'value':PR})
                            calls=[]
                            def command(argv,**kwargs):
                                argv=list(argv);calls.append(argv)
                                if '--web' in argv:
                                    if failure=='browser':raise RuntimeError('browser unavailable')
                                    return ''
                                if failure=='offline':raise RuntimeError('GitHub offline')
                                if failure=='quota':raise w.GitHubRateLimit('fixture quota',retry_at=5000.)
                                return json.dumps(PR if argv[1:3]==['pr','view'] else [PR])
                            error=''
                            with patch.object(w.time,'time',return_value=1000.25),patch.object(w,'now',return_value=CLOCK):
                                try:
                                    if native:
                                        invoke_rpc({'op':'action.call','task':task,'repo':'owner/repo','state_root':directory,'root':directory,'clock':CLOCK},
                                                   lambda event:{'stdout':command(event['argv']),'stderr':'','exit':0})
                                    else:
                                        dashboard=tui.Dashboard(curses,state,'owner/repo',1,root=root)
                                        dashboard.submit=lambda loader,done,**kwargs:done(loader())
                                        with patch.object(w,'raw_command',side_effect=command):dashboard.open_pr(task)
                                except RuntimeError as exception:error=str(exception)
                            if failure=='offline' and not cached:self.assertEqual(error,'GitHub offline')
                            elif failure=='quota' and not cached:self.assertEqual(error,'fixture quota')
                            elif failure=='browser':self.assertEqual(error,'browser unavailable')
                            else:self.assertEqual(error,'')
                            observed.append({'calls':calls,'error':error,'cache':state.get_meta(key)})
                    self.assertEqual(observed[1],observed[0])

    def test_cached_quota_error_preserves_saved_value_and_authoritative_deadline(self):
        observed=[]
        for native in (False,True):
            with tempfile.TemporaryDirectory() as directory,patch.object(w.time,'time',return_value=1000.25):
                state=w.State('owner/repo',Path(directory));state.set_meta('tui:probe',{'value':PR})
                calls=[]
                def quota(*args,**kwargs):
                    calls.append(True)
                    raise w.GitHubRateLimit('fixture quota',retry_at=5000.)
                values=[]
                for _ in range(2):
                    if native:
                        value=invoke_rpc({'op':'read.call','method':'cache','key':'probe','argv':['api','probe'],
                                          'repo':'owner/repo','state_root':directory,'clock':CLOCK},quota)
                        values.append((value['value'],value['error']))
                    else:values.append(tui.ViewCache(state).read('probe',quota))
                self.assertEqual(len(calls),1)
                self.assertEqual(state.get_meta('tui:probe')['retry_at'],5000.)
                self.assertEqual(values,[(PR,'fixture quota')]*2)
                observed.append(state.get_meta('tui:probe'))
        self.assertEqual(observed[1],observed[0])

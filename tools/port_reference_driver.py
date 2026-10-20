#!/usr/bin/env python3
"""Test-only JSON adapter for the pinned Python runtime; no desktop protocol."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source))
    import worklane as w
    import worklane_tui as tui
    from worklane_harness import OpenCodeTurn, validate_session_id
    request = json.load(sys.stdin)
    # Explicit clock/ID dependency injection; never normalize behavior after execution.
    w.now = lambda: request.get('clock', '2026-10-20T00:00:00+00:00')
    ids = iter(range(1, 10000))
    w.uuid.uuid4 = lambda: uuid.UUID(int=next(ids))
    op = request['op']
    try:
        if op == 'config.validate':
            output = w.validate_config(request['values'])
        elif op == 'guard.invoke':
            from port_failure_cases import invoke
            outcome, code = invoke(request)
            print(json.dumps(outcome,ensure_ascii=False))
            return code
        elif op == 'terminal.initialize':
            from unittest.mock import patch
            called = []
            with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stdout, 'isatty', return_value=True):
                if request['fault'] == 'missing-backend':
                    with patch.dict(sys.modules, {'curses':None}):
                        tui.terminal_ui(lambda *_: called.append(True))
                else:
                    import curses
                    error = KeyboardInterrupt() if request['fault'] == 'interrupt' else curses.error(request['message'])
                    with patch.object(curses, 'wrapper', side_effect=error):
                        tui.terminal_ui(lambda *_: called.append(True))
            output = {'callback_called':bool(called)}
        elif op == 'text.wrap':
            output = {'wrapped':tui.wrapped(request['value'], request['width']),
                      'clipped':tui.clipped(request['value'], request['width'])}
        elif op == 'log.readable':
            output = w.readable_log(request['stream'], request['content'])
        elif op == 'session.validate':
            output = validate_session_id(request['backend'], request['id'])
        elif op == 'opencode.decode':
            turn = OpenCodeTurn()
            events = [turn.events(event) for event in request['events']]
            output = {'events':events, 'result':turn.result()}
        elif op == 'controller.run':
            output = w.main(request['argv'])
        elif op == 'agent.run':
            state = w.State('owner/repo')
            task = {'number':1,'phase':'building','branch':'codex/1-worklane','base':'main','issue':{'title':'界 fixture'}}
            with w.locked(state.path / 'runner.lock') as fd:
                backend = w.AgentBackend(None, Path.cwd(), state, fd, harness=request['backend'])
                output = {'result':backend.run(request['stage'],task,Path.cwd()),
                          'session':state.session(1,w.ROLES[request['stage']])}
        elif op == 'state.trace':
            state = w.State('owner/repo')
            task = request.get('task', {'number':1,'phase':'building'})
            state.save(task)
            output = []
            with w.locked(state.path / 'runner.lock'):
                run = state.begin_run(task,request.get('stage','build'),Path.cwd())
                for action in request['actions']:
                    kind = action['kind']
                    if kind == 'session':
                        state.attach_session(run, action['id'], action.get('model',w.MODEL), action.get('backend','codex'))
                    elif kind == 'queue':
                        state.queue_input(run,action['text'])
                    elif kind == 'advance':
                        output.append(state.advance_run(run,action.get('result')))
                    elif kind == 'recover':
                        state.recover_runs()
                    elif kind == 'reopen':
                        state = w.State('owner/repo')
                    elif kind == 'snapshot':
                        output.append({'tasks':state.all(),'runs':state.runs(),'logs':state.log_entries(),
                                       'session':state.session(1,w.ROLES[request.get('stage','build')])})
                    else:
                        raise ValueError('Unknown trace action: ' + kind)
        else:
            raise ValueError('Unknown runtime operation: ' + op)
        print(json.dumps({'ok':output},ensure_ascii=False))
        return 0
    except (RuntimeError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

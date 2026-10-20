"""Test-only access to the frozen Python shared security boundaries."""
from pathlib import Path
import argparse


def selected_board(w, request):
    board = object.__new__(w.Board)
    board.repo = request.get('repo', 'owner/repo')
    board.project = {'id':'project','number':2,'url':'board-url'}
    board.owner, board.number = 'owner', 2
    board.path = Path.cwd()
    board.dangerously_allow_public_board = request.get('override', False)
    board.field = {'id':'status-id','options':[{'id':'ready','name':w.READY}]}
    board.options = {w.READY.casefold():'ready',w.PROGRESS.casefold():'progress'}
    board.required_statuses = ()
    return board


def invoke(w, request):
    root = Path(request.get('root', Path.cwd()))
    state = w.State(request.get('repo','owner/repo'), request.get('state_root'))
    board = selected_board(w, request)
    runner = w.Runner(argparse.Namespace(base='main'), state.repo, root, state, board, None)
    task, item = request.get('task', {'number':1}), request.get('item', {})
    method = request['method']
    if method == 'report': return board.security_report()
    if method == 'open':
        return w.Board(state.repo, root, 'owner/2', load_fields=False,
                       dangerously_allow_public_board=request.get('override',False),
                       check_only=request.get('check_only',False)).security
    if method == 'safe': return board.require_safe()
    if method == 'add': return board.add_issue('Title','')
    if method == 'ensure_review': return board.ensure_review_status()
    if method == 'move': return board.move(item,w.PROGRESS,w.READY)
    if method == 'feedback': return runner.human_feedback(task)
    if method == 'trusted': return runner.require_trusted_feedback(task)
    if method == 'before_turn': return runner.before_turn(request.get('stage','build'),task)
    if method == 'agent_result': return runner.agent_result(task,'build',root,'')
    if method == 'recover': return runner.recover(request.get('items',[]),'worker-1')
    if method == 'pick': return runner.pick(item,'worker-1')
    if method == 'poll': return runner.poll_human_feedback(request.get('items',[]))
    if method == 'post': return runner.post(task,'review',{})
    if method == 'restack': return runner.restack(task,[],item)
    if method == 'save_editor':
        import worklane_tui as tui
        return tui.save_editor(state.repo,{'board':board,'item':item,'title':'Edit','body':'','status':w.READY})
    raise ValueError('Unknown shared security operation: '+method)

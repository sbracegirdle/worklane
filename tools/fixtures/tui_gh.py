#!/usr/bin/env python3
"""Controlled external gh boundary for native PTY tests; never accesses GitHub."""
import json
import os
from pathlib import Path
import sys
import time
root = Path(os.environ['TUI_FIXTURE'])
args = sys.argv[1:]
stdin = sys.stdin.read() if '--input' in args or '--body-file' in args else ''
with (root/'gh-calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'args': args, 'stdin': stdin})+'\n')
mode = (root/'mode').read_text() if (root/'mode').exists() else ''
issue = json.loads((root/'issue.json').read_text())
def page(nodes, more=False):
    return {'nodes': nodes, 'totalCount': len(nodes) if not more else 2,
            'pageInfo': {'hasNextPage': more, 'endCursor': 'next' if more else None}}
if args[:2] == ['api', '-i']:
    request = json.loads(stdin); query = request['query']; variables = request['variables']
    if 'viewerCanUpdate' in query:
        result = {'node': {'public': mode == 'public', 'viewerCanUpdate': mode != 'revoked'},
                  'repository': {'viewerPermission': 'ADMIN'}}
    elif 'comments(first:100' in query:
        if mode == 'slow':
            (root/'slow-entered').touch()
            time.sleep(30)
        second = variables.get('cursor') == 'next'
        connection = page([{'body': 'Second paginated comment' if second else 'First comment',
                            'author': {'login': 'owner'}}], not second)
        connection['totalCount'] = 2
        result = {'repository': {'issue': {'comments': connection}}}
    elif 'items(first:100' in query:
        content = dict(issue, repository={'nameWithOwner': 'owner/repo'}, issueDependenciesSummary={'blockedBy': 0})
        result = {'node': {'items': page([{'id': 'item', 'content': content,
                    'fieldValueByName': {'name': 'Ready for work'}, 'links': {'pullRequests': page([])}}])}}
    elif 'project { id }' in query:
        result = {'node': {'project': {'id': 'project'}, 'fieldValueByName': {'name': 'Ready for work'}}}
    else:
        raise AssertionError(query)
    print(json.dumps({'data': result}))
elif args[:2] == ['project', 'view']:
    print(json.dumps({'id': 'project', 'number': 2, 'title': 'repo', 'url': 'https://github.com/users/owner/projects/2'}))
elif args[:2] == ['project', 'field-list']:
    print(json.dumps({'fields': [{'id': 'status', 'name': 'Status', 'options': [
        {'id': 'draft', 'name': 'Draft'}, {'id': 'ready', 'name': 'Ready for work'}, {'id': 'done', 'name': 'Done'}]}]}))
elif args[:2] == ['issue', 'view']:
    if '--web' not in args:
        print(json.dumps(issue))
elif args[:2] == ['issue', 'edit']:
    if mode == 'conflict':
        raise AssertionError('conflicted edit must be refused before writing')
    issue['title'] = args[args.index('--title')+1]; issue['body'] = stdin
    (root/'issue.json').write_text(json.dumps(issue))
elif args[:2] == ['issue', 'create']:
    issue['title'] = args[args.index('--title')+1]; issue['body'] = stdin
    (root/'issue.json').write_text(json.dumps(issue)); print(issue['url'])
elif args[:2] == ['project', 'item-add']:
    print(json.dumps({'id': 'item'}))
elif args[:2] == ['project', 'item-edit']:
    pass
elif args[:2] == ['pr', 'view']:
    if '--web' not in args:
        print(json.dumps({'number': 12, 'url': 'https://github.com/owner/repo/pull/12', 'state': 'OPEN', 'isDraft': True}))
else:
    raise AssertionError(args)

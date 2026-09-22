"""Fixed synthetic job. Never executes application code or receives a broker credential."""
import base64
import json
import os
import pathlib
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

REPOSITORY = re.compile(r'Ligerian-labs/llpm-p00d-public-[0-9]{14}-[0-9a-f]{6}')
SHA = re.compile(r'[0-9a-f]{40}')
REFS = {'unprotected', 'managed-head', 'release-test'}


def validate(value):
    assert set(value) == {'repository', 'repository_id', 'base', 'head', 'candidate', 'tree', 'context'}
    assert REPOSITORY.fullmatch(value['repository'])
    assert type(value['repository_id']) is int and value['repository_id'] > 0
    assert all(SHA.fullmatch(value[key]) for key in ('base', 'head', 'candidate', 'tree'))
    assert re.fullmatch(r'llpm-p00d-[0-9a-f]{12}', value['context'])
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Unexpected redirect')


def main():
    raw = os.environ['PROOF_PROFILE']
    assert len(raw.encode()) <= 2048
    plan = validate(json.loads(raw))
    assert plan['repository'] == os.environ['GITHUB_REPOSITORY']
    assert str(plan['repository_id']) == os.environ['GITHUB_REPOSITORY_ID']
    assert plan['base'] == os.environ['GITHUB_SHA']
    mode = os.environ['PROOF_MODE']
    assert mode in ('verifier', 'writer')
    token = os.environ['PROOF_JOB_TOKEN']
    endpoint = '/repos/' + plan['repository']
    evidence = {'mode': mode, 'run_id': os.environ['GITHUB_RUN_ID'], 'workflow_sha': plan['base'], 'repository_id': plan['repository_id'], 'checks': [], 'requests': []}

    def api(method, suffix, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request('https://api.github.com' + endpoint + suffix, data=data, method=method, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            response = urllib.request.build_opener(NoRedirect).open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(1024 * 1024 + 1)
            assert len(body) <= 1024 * 1024
            result = json.loads(body) if body else None
            evidence['requests'].append({'method': method, 'path': suffix, 'status': response.code, 'request_id': response.headers.get('X-GitHub-Request-Id')})
            return response.code, result

    def check(name, valid):
        evidence['checks'].append({'name': name, 'passed': bool(valid)})
        if not valid:
            raise RuntimeError(name)

    def read(ref):
        assert ref in REFS
        status, value = api('GET', '/git/ref/heads/' + ref)
        check('read_' + ref, status == 200)
        return value['object']['sha']

    def update(ref):
        assert ref in REFS
        return api('PATCH', '/git/refs/heads/' + ref, {'sha': plan['candidate'], 'force': False})[0]

    try:
        status, repo = api('GET', '')
        check('exact_owned_public_repository', status == 200 and repo['id'] == plan['repository_id'] and repo['full_name'] == plan['repository'] and repo['private'] is False and repo['archived'] is False)
        status, commit = api('GET', '/git/commits/' + plan['candidate'])
        check('exact_merge_identity', status == 200 and commit['sha'] == plan['candidate'] and commit['tree']['sha'] == plan['tree'] and [parent['sha'] for parent in commit['parents']] == [plan['base'], plan['head']])
        status, tree = api('GET', '/git/trees/' + plan['tree'] + '?recursive=1')
        check('bounded_complete_synthetic_tree', status == 200 and not tree.get('truncated') and len(tree['tree']) == 5 and {entry['path'] for entry in tree['tree'] if entry['type'] == 'blob'} == {'.github/workflows/proof.yml', 'proof.py', 'message.txt'} and all(entry['mode'] in ('040000', '100644') for entry in tree['tree']))
        message = next(entry for entry in tree['tree'] if entry['path'] == 'message.txt')
        status, blob = api('GET', '/git/blobs/' + message['sha'])
        check('fixed_candidate_content', status == 200 and blob['encoding'] == 'base64' and base64.b64decode(blob['content']) == b'synthetic approved change\n')
        if mode == 'verifier':
            check('verifier_control_initial_base', read('unprotected') == plan['base'])
            check('verifier_contents_write_denied', update('unprotected') == 403 and read('unprotected') == plan['base'])
            status, result = api('POST', '/check-runs', {'name': plan['context'], 'head_sha': plan['candidate'], 'status': 'completed', 'conclusion': 'success', 'external_id': os.environ['GITHUB_RUN_ID'], 'output': {'title': 'Synthetic exact candidate verified', 'summary': 'Fixed synthetic content and exact base/head/tree. No build or production provenance claim.'}})
            check('observed_github_actions_check', status == 201 and result['app']['id'] == 15368 and result['app']['slug'] == 'github-actions' and result['head_sha'] == plan['candidate'] and result['conclusion'] == 'success')
            evidence['check_run_id'] = result['id']
            evidence['app_id'] = result['app']['id']
        else:
            with tempfile.TemporaryDirectory(prefix='p00d-job-') as directory:
                root = pathlib.Path(directory)
                askpass = root / 'askpass.py'
                askpass.write_text('#!/usr/bin/env python3\nimport os,sys\nprint("x-access-token" if "username" in sys.argv[1].lower() else os.environ["PROOF_JOB_TOKEN"])\n')
                askpass.chmod(0o700)
                environment = {'PATH': os.environ['PATH'], 'PROOF_JOB_TOKEN': token, 'GIT_ASKPASS': str(askpass), 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
                remote = 'https://github.com/' + plan['repository'] + '.git'

                def git(*args):
                    return subprocess.run(['git', '-c', 'credential.helper=', *args], cwd=root, env=environment, capture_output=True, timeout=30)

                check('fresh_empty_git_directory', git('init', '.').returncode == 0)
                check('fetch_exact_candidate', git('fetch', '--no-tags', '--depth=3', remote, plan['candidate']).returncode == 0)

                def push(ref):
                    assert ref in REFS
                    return git('push', '--porcelain', '--force-with-lease=refs/heads/' + ref + ':' + plan['base'], remote, plan['candidate'] + ':refs/heads/' + ref)

                check('writer_unprotected_positive_control', read('unprotected') == plan['base'] and push('unprotected').returncode == 0 and read('unprotected') == plan['candidate'])
                for ref in ('managed-head', 'release-test'):
                    check(ref + '_before', read(ref) == plan['base'])
                    rejected = push(ref)
                    policy_denial = any(marker in rejected.stderr.lower() for marker in (b'gh006', b'gh013', b'protected branch'))
                    check(ref + '_git_denied', rejected.returncode != 0 and policy_denial and read(ref) == plan['base'])
                    check(ref + '_rest_denied', update(ref) in (403, 422) and read(ref) == plan['base'])
        evidence['outcome'] = 'passed'
    except Exception as error:
        evidence['outcome'] = 'stopped'
        evidence['failure_type'] = type(error).__name__
        raise RuntimeError('Synthetic proof stopped; inspect safe receipt') from None
    finally:
        print('P00D_RECEIPT ' + json.dumps(evidence, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    main()

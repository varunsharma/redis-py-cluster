"""Pytest replay checkpoints. Compatible with the application's Python 2.7 floor.

Activate explicitly with ``-p ci_replay``. History contains test IDs and CI
provenance only; it never stores assertions, logs, environment dumps or secrets.
"""
from __future__ import print_function

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import threading

import pytest

MAX_BYTES = 8 * 1024 * 1024
MAX_TESTS = 25000
_SESSION = None


def _git(*args):
    # Commands are fixed; revisions from cached state are validated before use.
    try:
        process = subprocess.Popen(['git'] + list(args), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        deadline = threading.Timer(5, process.kill)
        deadline.start()
        try:
            output, _ = process.communicate()
        finally:
            deadline.cancel()
        return output.decode('utf-8', 'replace').strip() if not process.returncode else None
    except OSError:
        return None


def _context():
    env = os.environ
    branch = (env.get('CI_REPLAY_BRANCH') or env.get('TRAVIS_PULL_REQUEST_BRANCH')
              or env.get('TRAVIS_BRANCH') or env.get('GITHUB_HEAD_REF')
              or env.get('GITHUB_REF_NAME') or env.get('CI_COMMIT_REF_NAME')
              or _git('rev-parse', '--abbrev-ref', 'HEAD') or '')
    return {key: str(value)[:400] for key, value in {
        'repository': env.get('TRAVIS_REPO_SLUG') or env.get('GITHUB_REPOSITORY') or env.get('CI_PROJECT_PATH') or '',
        'branch': branch,
        'pr': env.get('TRAVIS_PULL_REQUEST') or env.get('CI_MERGE_REQUEST_IID') or
              (env.get('GITHUB_REF', '').split('/')[2] if env.get('GITHUB_REF', '').startswith('refs/pull/') else ''),
        'sha': _git('rev-parse', 'HEAD') or '',
        'run': env.get('TRAVIS_BUILD_ID') or env.get('GITHUB_RUN_ID') or env.get('CI_PIPELINE_ID') or '',
        'job': env.get('TRAVIS_JOB_ID') or env.get('GITHUB_JOB') or env.get('CI_JOB_ID') or '',
        'lane': env.get('CI_REPLAY_LANE', 'default'),
        'python': platform.python_version(),
    }.items()}


def _valid_ids(value):
    return (isinstance(value, list) and len(value) <= MAX_TESTS
            and all(isinstance(item, type(u'')) and len(item) <= 2000 for item in value))


class Replay(object):
    def __init__(self):
        self.context = _context()
        scope = [self.context[key] for key in
                 ('repository', 'branch', 'pr', 'lane', 'python')]
        digest = hashlib.sha256(json.dumps(scope).encode('utf-8')).hexdigest()[:32]
        self.directory = os.path.abspath(os.environ.get('CI_REPLAY_CACHE_DIR', '.ci-replay-cache'))
        self.path = os.path.join(self.directory, digest + '.json')
        self.state = {'version': 1, 'failed': [], 'pending': [], 'risk': {}, 'contexts': []}
        if os.path.islink(self.directory) or os.path.islink(self.path):
            raise pytest.UsageError('Invalid replay history: symbolic link')
        if os.path.exists(self.path):
            self.load()
        self.previous_sha = self.state['contexts'][-1].get('sha', '') if self.state['contexts'] else ''
        self.state['contexts'] = (self.state['contexts'] + [self.context])[-12:]
        self.failed_this_run = set()
        self.passed_calls = set()
        self.passed_collectors = set()
        self.failed_collectors = set()

    def load(self):
        try:
            if os.path.getsize(self.path) > MAX_BYTES:
                raise ValueError('oversized state')
            with open(self.path) as stream:
                value = json.load(stream)
            if (value.get('version') != 1 or not _valid_ids(value.get('failed'))
                    or not _valid_ids(value.get('pending'))
                    or not isinstance(value.get('risk'), dict)
                    or len(value['risk']) > MAX_TESTS
                    or not _valid_ids(list(value['risk']))
                    or not all(isinstance(n, int) and 0 <= n <= 1000000 for n in value['risk'].values())
                    or not isinstance(value.get('contexts'), list) or len(value['contexts']) > 12
                    or not all(isinstance(c, dict) and isinstance(c.get('sha', ''), type(u''))
                               for c in value['contexts'])):
                raise ValueError('invalid schema')
            self.state = value
        except (ValueError, TypeError, AttributeError, IOError, OSError) as exc:
            raise pytest.UsageError('Invalid replay history: ' + str(exc))

    def save(self):
        if len(self.state['failed']) > MAX_TESTS or len(self.state['pending']) > MAX_TESTS:
            raise pytest.UsageError('Replay history exceeds supported test count')
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)
        payload = json.dumps(self.state, sort_keys=True).encode('utf-8')
        if len(payload) > MAX_BYTES:
            raise pytest.UsageError('Replay history exceeds supported byte count')
        descriptor, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=self.directory)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def order(self, items):
        failed, pending = set(self.state['failed']), set(self.state['pending'])
        risky = self.state['risk']
        changed = None
        if re.match(r'^[0-9a-f]{40}$', self.previous_sha):
            changes = _git('diff', '--no-ext-diff', '--name-only', self.previous_sha, 'HEAD', '--')
            # Unknown ancestry conservatively covers the whole suite.
            changed = set(changes.splitlines()) if changes is not None else None

        def priority(item):
            node = item.nodeid
            if node in failed or any(node.startswith(n + '::') for n in failed):
                return (0, 0)
            if node in pending:
                return (1, 0)
            # Without a verified dependency map, a source change can affect any test.
            if changed is None or changed:
                return (2, -risky.get(node, 0))
            return (3, -risky.get(node, 0))

        items.sort(key=priority)
        collected = [item.nodeid for item in items]
        seen = set()
        self.state['pending'] = [n for n in collected + self.state['pending']
                                 if not (n in seen or seen.add(n))]
        self.save()  # A failed checkpoint prevents all test execution.

    def collection_report(self, report):
        if report.failed:
            self.failed_collectors.add(report.nodeid)
            if report.nodeid not in self.state['failed']:
                self.state['failed'].append(report.nodeid)
            self.save()
        elif report.passed:
            self.passed_collectors.add(report.nodeid)

    def finish(self):
        fixed_collectors = self.passed_collectors - self.failed_collectors
        self.state['failed'] = [n for n in self.state['failed'] if n not in fixed_collectors]
        self.save()

    def report(self, report):
        node = report.nodeid
        if report.when == 'call' and report.passed and not hasattr(report, 'wasxfail'):
            self.passed_calls.add(node)
        if report.failed:
            self.failed_this_run.add(node)
            if node not in self.state['failed']:
                self.state['failed'].append(node)
            self.state['risk'][node] = min(1000000, self.state['risk'].get(node, 0) + 1)
            self.save()  # Persist observed failures before pytest handles fail-fast.
        if report.when == 'teardown':
            self.state['pending'] = [n for n in self.state['pending'] if n != node]
            if node in self.passed_calls and node not in self.failed_this_run:
                self.state['failed'] = [n for n in self.state['failed'] if n != node]
            self.save()


def pytest_configure(config):
    global _SESSION
    config.option.maxfail = 1
    _SESSION = Replay()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    _SESSION.order(items)


def pytest_runtest_logreport(report):
    _SESSION.report(report)


def pytest_collectreport(report):
    _SESSION.collection_report(report)


def pytest_sessionfinish(session, exitstatus):
    _SESSION.finish()

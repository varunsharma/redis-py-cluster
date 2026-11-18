"""Isolated replay contracts; these tests do not need a Redis service."""
import json
import os
from pathlib import Path
import subprocess
import sys

PLUGIN = Path(__file__).resolve().parents[1]


def run_suite(tmp_path, source, **extra_env):
    (tmp_path / 'test_sample.py').write_text(source)
    env = dict(os.environ, PYTHONPATH=str(PLUGIN),
               CI_REPLAY_BRANCH='feature/replay', CI_REPLAY_LANE='unit',
               CI_REPLAY_CACHE_DIR=str(tmp_path / 'history'))
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'ci_replay',
         '--confcutdir=' + str(tmp_path), '-q'], cwd=str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)


def history(tmp_path):
    return json.loads(next((tmp_path / 'history').glob('*.json')).read_text())


def test_first_failure_stops_and_checkpoint_survives(tmp_path):
    result = run_suite(tmp_path, 'def test_a(): assert False\ndef test_b(): assert False\n')
    assert result.returncode == 1, result.stdout
    state = history(tmp_path)
    assert state['failed'] == ['test_sample.py::test_a']
    assert state['pending'] == ['test_sample.py::test_b']
    assert state['contexts'][-1]['branch'] == 'feature/replay'


def test_known_failure_runs_first(tmp_path):
    source = 'def test_a(): pass\ndef test_b(): assert False\ndef test_c(): assert False\n'
    assert run_suite(tmp_path, source).returncode == 1
    result = run_suite(tmp_path, source.replace('def test_a(): pass', 'def test_a(): assert False'))
    assert result.returncode == 1, result.stdout
    assert 'test_sample.py::test_b' in result.stdout
    assert history(tmp_path)['failed'] == ['test_sample.py::test_b']


def test_replay_scope_includes_repository_branch_and_lane(tmp_path):
    assert run_suite(tmp_path, 'def test_a(): assert False\n',
                     TRAVIS_REPO_SLUG='org/first').returncode == 1
    result = run_suite(tmp_path, 'def test_a(): pass\n',
                       TRAVIS_REPO_SLUG='org/second')
    assert result.returncode == 0, result.stdout
    assert len(list((tmp_path / 'history').glob('*.json'))) == 2


def test_context_records_pr_branch_sha_run_and_job(tmp_path):
    result = run_suite(
        tmp_path, 'def test_a(): assert False\n',
        CI_REPLAY_BRANCH='topic/replay',
        TRAVIS_PULL_REQUEST='123', TRAVIS_PULL_REQUEST_BRANCH='topic/replay',
        TRAVIS_BUILD_ID='456', TRAVIS_JOB_ID='789')
    assert result.returncode == 1, result.stdout
    context = history(tmp_path)['contexts'][-1]
    assert context['pr'] == '123'
    assert context['branch'] == 'topic/replay'
    assert context['run'] == '456'
    assert context['job'] == '789'


def test_pr_histories_are_isolated(tmp_path):
    assert run_suite(tmp_path, 'def test_a(): assert False\n',
                     TRAVIS_PULL_REQUEST='123').returncode == 1
    result = run_suite(tmp_path, 'def test_a(): pass\n',
                       TRAVIS_PULL_REQUEST='124')
    assert result.returncode == 0, result.stdout
    assert len(list((tmp_path / 'history').glob('*.json'))) == 2

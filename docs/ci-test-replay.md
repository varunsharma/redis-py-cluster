# CI test replay

Travis and tox activate `ci_replay.py` with `-p ci_replay --maxfail=1` and a
600-second per-test deadline. The Travis test command also has a 45-minute
outer deadline with a bounded termination grace period.
The plugin checkpoints collected test IDs before executing tests and updates
history immediately when a test or collection fails. A checkpoint write error
fails the invocation before further tests run. The normal coverage command and
entire existing test suite remain in place.

## Order

1. Previously failed tests and tests in a previously failed collection file.
2. Collected tests left unfinished by an earlier invocation.
3. Tests affected by changes, with historically failing tests first.
4. Remaining tests, ordered by their recorded failure counts.

The repository has no verified source-to-test dependency map. Any changed source
or unavailable Git ancestry conservatively classifies the entire suite as
potentially affected. This can make the anticipated group large; it does not
skip the rest of the suite. Pytest stops at its first failure, including setup,
teardown and collection errors. Tests that pass during replay are part of the
same coverage run and are not run twice.

## Storage and provenance

Travis caches `.ci-replay-cache` alongside its existing pip cache. Each history
file is scoped by repository, branch, Python version and explicit test lane. Travis lanes
also include Redis and hiredis versions; tox uses its environment name. History
contains test IDs, pending work, bounded failure counts and the last 12 run
contexts: repository, branch, PR, commit, run, job, lane and Python version.
Assertions, exception bodies, logs and environment dumps are not stored.

Checkpoints use an atomic rename after flushing the file. Invalid, oversized or
symlinked history fails closed. A history file supports 25,000 test IDs and up
to 8 MiB. Missing history starts a full run. Removed or unselected test IDs are
not treated as evidence that a test passed.
Skipped tests and expected failures also retain any previous unresolved failure;
only an actual passing test call with no setup or teardown failure clears it.

The cache is best effort: eviction, cache upload failures, concurrent updates
and runner loss can lose information. Travis documents that pull-request builds
can read caches, but only normal pushes update the stored cache; failures from
a pull-request run therefore may not be present in the next pull-request run.
It is not a durable error ledger and cannot guarantee perfect replay across
runs. Travis also runs matrix jobs in parallel; this plugin stops tests only in
the current job and does not cancel other jobs in the same build. Repository
settings and build orchestration control those behaviors.

Travis documents cache upload after the script phase, including failed test
runs: [cache lifecycle](https://docs.travis-ci.com/user/caching/).
Pytest's native fail-fast behavior is described in its
[test execution documentation](https://docs.pytest.org/en/stable/how-to/failures.html).

## Focused adapter checks

Run `python -m pytest ci_replay_tests/check_replay.py --confcutdir=ci_replay_tests -q -x`
with modern Python and pytest. These isolated contracts require no Redis service
and are deliberately named outside normal application test discovery. The
plugin itself preserves the application's Python 2.7 syntax floor.

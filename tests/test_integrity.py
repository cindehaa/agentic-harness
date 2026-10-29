"""Tamper detection against real Docker: snapshot/changed spot edits and deletes; a cheat still scores, but is flagged."""

import subprocess

import pytest

from agentic_harness.benchmarks import PythonTasks
from agentic_harness.environments import DockerEnvironment
from agentic_harness.integrity import changed, snapshot

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

bench = PythonTasks()
TASK = bench.tasks()[0]  # py-001
PROTECTED = bench.protected_files(TASK)  # ["/workspace/test_solution.py"]


@pytest.fixture(scope="module", autouse=True)
def image():
    bench.setup([TASK])


@pytest.fixture
def env():
    with DockerEnvironment(TASK.image) as env:
        yield env


def test_unchanged_is_empty(env):
    before = snapshot(env, PROTECTED)
    env.write_file("/workspace/solution.py", "def add():\n    return 4\n")  # unprotected files may change freely
    assert changed(before, snapshot(env, PROTECTED)) == []


def test_modified_is_listed(env):
    before = snapshot(env, PROTECTED)
    env.write_file(PROTECTED[0], "def test_ok():\n    assert True\n")
    assert changed(before, snapshot(env, PROTECTED)) == PROTECTED


def test_deleted_is_listed(env):
    before = snapshot(env, PROTECTED)
    env.exec(["rm", PROTECTED[0]])
    after = snapshot(env, PROTECTED)
    assert after[PROTECTED[0]] is None
    assert changed(before, after) == PROTECTED


def test_cheater_scores_but_is_flagged(env):
    before = snapshot(env, PROTECTED)
    env.write_file("/workspace/solution.py", "def add():\n    return 5\n")  # wrong
    env.write_file("/workspace/test_solution.py", "def test_ok(): assert True\n")  # the benchmark's own loophole
    after = snapshot(env, PROTECTED)
    assert bench.grade(TASK, env, ()).value is True  # faithful: the score is what the benchmark says
    assert changed(before, after) == ["/workspace/test_solution.py"]  # ...but the harness notices

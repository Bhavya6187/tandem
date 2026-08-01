"""bench/tasks.toml is the experiment's pin: exactly which ten tasks run, and
with which per-family knobs. These tests are the guard against silent drift —
a task disappearing, an id changing, a timeout being edited to something that
would make a family unmeasurable."""

import pytest
from conftest import BENCH_DIR, load_bench_module

runner = load_bench_module("runner")

SWEBENCH_IDS = [
    "django__django-16263",
    "django__django-11885",
    "astropy__astropy-13398",
    "sympy__sympy-16597",
    "scikit-learn__scikit-learn-25102",
]
REPOQA_IDS = ["repoqa-python-black", "repoqa-typescript-express", "repoqa-go-fzf"]
LCA_IDS = ["lca-1", "lca-2"]


@pytest.fixture
def cfg():
    return runner.load_tasks(BENCH_DIR / "tasks.toml")


def test_pins_exactly_ten_tasks(cfg):
    assert [t["id"] for t in cfg.tasks] == SWEBENCH_IDS + REPOQA_IDS + LCA_IDS


def test_ids_are_unique_and_path_safe(cfg):
    ids = [t["id"] for t in cfg.tasks]
    assert len(set(ids)) == len(ids)
    for i in ids:
        assert "/" not in i and ".." not in i and i.strip() == i


def test_family_split(cfg):
    fams = [t["family"] for t in cfg.tasks]
    assert fams.count("swebench") == 5
    assert fams.count("repoqa") == 3
    assert fams.count("lca") == 2
    assert set(fams) <= set(runner.FAMILIES)


def test_swebench_fields(cfg):
    tasks = [t for t in cfg.tasks if t["family"] == "swebench"]
    assert [t["instance_id"] for t in tasks] == SWEBENCH_IDS
    assert {t["repo"] for t in tasks} == {
        "django/django", "astropy/astropy", "sympy/sympy",
        "scikit-learn/scikit-learn",
    }
    for t in tasks:
        assert t["arm64_image"] is True
        assert t["dataset"] == "princeton-nlp/SWE-bench_Verified"


def test_repoqa_fields(cfg):
    tasks = [t for t in cfg.tasks if t["family"] == "repoqa"]
    got = [(t["language"], t["repo"], t["needle_name"]) for t in tasks]
    assert got == [
        ("python", "psf/black", "_merge_string_group"),
        ("typescript", "expressjs/express", "sendfile"),
        ("go", "junegunn/fzf", "newRange"),
    ]


def test_lca_rows_are_pinned(cfg):
    """The pin Task 3 made, live, against the datasets-server.

    Was "these are still empty placeholders" until Task 3 filled them. The
    shas and the ground-truth file lists are the whole reproducibility story
    for this family — an edit here silently changes what is being measured, so
    it has to be a deliberate change to this test too."""
    tasks = [t for t in cfg.tasks if t["family"] == "lca"]
    for t in tasks:
        assert set(runner.LCA_SCHEMA) <= set(t)
        assert t["f1_threshold"] == 0.5
        assert t["hub_config"] == "py" and t["hub_split"] == "test"
        assert len(t["base_sha"]) == 40 and len(t["head_sha"]) == 40
        assert len(t["expected_files"]) >= 2
        assert all("/" in f and not f.startswith("/") for f in t["expected_files"])
    assert [(t["hub_row_index"], t["repo"]) for t in tasks] == [
        (42, "pre-commit/pre-commit"),
        (44, "tweepy/tweepy"),
    ]
    assert tasks[0]["expected_files"] == [
        "pre_commit/languages/helpers.py", "pre_commit/languages/node.py",
        "pre_commit/languages/ruby.py", "tests/languages/helpers_test.py"]
    assert tasks[1]["expected_files"] == [
        "tweepy/binder.py", "tweepy/cursor.py", "tweepy/parsers.py"]


def test_run_defaults(cfg):
    assert cfg.run["repeats"] == 1
    assert cfg.run["model"] == ""
    assert cfg.run["arms"] == ["a", "b"]
    assert cfg.run["timeout_s"] == {"swebench": 3600, "repoqa": 900, "lca": 900}
    assert "--output-format" not in cfg.run["claude_flags"]  # runner owns those
    assert cfg.run["smoke_task"] in REPOQA_IDS


def test_timeout_for_every_family(cfg):
    for fam in runner.FAMILIES:
        assert runner.timeout_for(cfg, fam) > 0


def test_pinned_tool_versions_match_the_verifiers_that_run_them(cfg):
    """[tools] is the operator-facing record of what the verifiers shell out to.

    A record kept by hand next to the code it describes is a record that goes
    stale, so it is asserted against the constants the verifiers actually use.
    Bump one, and this fails until the other is bumped too."""
    import tomllib

    with open(BENCH_DIR / "tasks.toml", "rb") as fh:
        tools = tomllib.load(fh)["tools"]
    swebench = runner.load_family("swebench", runner.DEFAULT_FAMILY_DIR)
    repoqa = runner.load_family("repoqa", runner.DEFAULT_FAMILY_DIR)
    assert tools["swebench"] == swebench.SWEBENCH_PIN
    assert tools["repoqa"] == repoqa.REPOQA_PIN
    assert tools["repoqa_tree_sitter"] == repoqa.TREE_SITTER_PIN
    assert tools["repoqa_release"] == repoqa.RELEASE_VERSION
    # and they are what the commands actually carry
    assert tools["swebench"] in swebench.HARNESS_CMD
    assert tools["repoqa_tree_sitter"] in repoqa.SCORER_CMD


# --- selection ---------------------------------------------------------------


def test_select_tasks_preserves_requested_order(cfg):
    got = runner.select_tasks(cfg, ["lca-1", "django__django-16263"])
    assert [t["id"] for t in got] == ["lca-1", "django__django-16263"]


def test_select_tasks_all(cfg):
    assert len(runner.select_tasks(cfg, None)) == 10


def test_select_unknown_task_names_it(cfg):
    with pytest.raises(runner.BenchError) as exc:
        runner.select_tasks(cfg, ["nope"])
    assert "nope" in str(exc.value)


# --- runnability -------------------------------------------------------------


def test_every_pinned_task_is_runnable(cfg):
    for t in cfg.tasks:
        assert runner.task_runnable(t) == (True, ""), t["id"]


def test_an_unfilled_lca_pin_is_still_refused_with_a_clear_message(cfg):
    """The shipped rows are pinned now, so blank one out to check the guard.

    It is the message a future re-pin lands on, and the only thing standing
    between an empty expected_files and a run that scores everything zero."""
    t = dict(next(t for t in cfg.tasks if t["family"] == "lca"),
             repo="", expected_files=[], hub_row_index=-1)
    ok, why = runner.task_runnable(t)
    assert ok is False
    assert "lca-1" in why and "repo" in why
    assert "Task 3" in why or "not pinned" in why


def test_load_tasks_rejects_unknown_family(tmp_path):
    p = tmp_path / "tasks.toml"
    p.write_text('[run]\n[[tasks]]\nid = "x"\nfamily = "martian"\n')
    with pytest.raises(runner.BenchError) as exc:
        runner.load_tasks(p)
    assert "martian" in str(exc.value)


def test_load_tasks_rejects_duplicate_ids(tmp_path):
    p = tmp_path / "tasks.toml"
    p.write_text(
        '[run]\n'
        '[[tasks]]\nid = "x"\nfamily = "repoqa"\n'
        '[[tasks]]\nid = "x"\nfamily = "repoqa"\n'
    )
    with pytest.raises(runner.BenchError) as exc:
        runner.load_tasks(p)
    assert "duplicate" in str(exc.value).lower()


def test_load_tasks_missing_file_is_a_bench_error(tmp_path):
    with pytest.raises(runner.BenchError):
        runner.load_tasks(tmp_path / "absent.toml")

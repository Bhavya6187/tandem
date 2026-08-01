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


def test_lca_placeholder_schema(cfg):
    tasks = [t for t in cfg.tasks if t["family"] == "lca"]
    for t in tasks:
        assert set(runner.LCA_SCHEMA) <= set(t)
        assert t["f1_threshold"] == 0.5
        # unfilled by construction: Task 3 pins the real rows
        assert t["repo"] == ""
        assert t["expected_files"] == []


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


def test_swebench_and_repoqa_are_runnable(cfg):
    for t in cfg.tasks:
        if t["family"] != "lca":
            assert runner.task_runnable(t) == (True, "")


def test_unfilled_lca_is_not_runnable_with_a_clear_message(cfg):
    t = next(t for t in cfg.tasks if t["family"] == "lca")
    ok, why = runner.task_runnable(t)
    assert ok is False
    assert "lca-1" in why and "repo" in why
    assert "Task 3" in why or "not pinned" in why


def test_filled_lca_becomes_runnable(cfg):
    t = dict(next(t for t in cfg.tasks if t["family"] == "lca"))
    t.update(hub_row_index=17, repo="a/b", base_sha="a" * 40, head_sha="b" * 40,
             expected_files=["src/x.py"])
    assert runner.task_runnable(t) == (True, "")


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

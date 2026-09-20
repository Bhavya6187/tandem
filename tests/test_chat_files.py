import subprocess

from tandem.chat.files import list_paths, match


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_git_repo_lists_tracked_and_untracked_but_not_ignored(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / "notes.md").write_text("")
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("")
    _git(tmp_path, "add", "src/app.py")
    assert list_paths(tmp_path) == [".gitignore", "notes.md", "src/", "src/app.py"]


def test_directories_are_listed_once_at_every_depth(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "one.txt").write_text("")
    (tmp_path / "a" / "b" / "two.txt").write_text("")
    assert list_paths(tmp_path) == ["a/", "a/b/", "a/b/one.txt", "a/b/two.txt"]


def test_outside_a_repo_walks_and_skips_dot_directories(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "blob").write_text("")
    (tmp_path / ".env").write_text("")
    assert list_paths(tmp_path) == [".env", "pkg/", "pkg/mod.py"]


def test_walk_is_capped(tmp_path):
    for n in range(20):
        (tmp_path / f"f{n:02}.txt").write_text("")
    assert len(list_paths(tmp_path, cap=5)) == 5


def test_a_missing_directory_lists_nothing(tmp_path):
    assert list_paths(tmp_path / "gone") == []


PATHS = ["README.md", "docs/", "docs/render-notes.md", "src/", "src/tandem/chat/render.py",
         "src/tandem/chat/window.py", "tests/test_chat_render.py"]


def test_empty_query_offers_the_shortest_paths_first():
    assert match("", PATHS, 3) == ["src/", "docs/", "README.md"]


def test_basename_prefix_beats_basename_substring():
    assert match("render", PATHS, 5) == ["docs/render-notes.md", "src/tandem/chat/render.py",
                                         "tests/test_chat_render.py"]


def test_basename_substring_beats_path_substring():
    assert match("chat", PATHS, 5) == ["tests/test_chat_render.py", "src/tandem/chat/render.py",
                                       "src/tandem/chat/window.py"]


def test_match_is_case_insensitive():
    assert match("readme", PATHS, 5) == ["README.md"]


def test_a_query_with_a_slash_matches_against_the_whole_path():
    assert match("chat/w", PATHS, 5) == ["src/tandem/chat/window.py"]


def test_subsequence_is_the_last_resort():
    assert match("stcw", PATHS, 5) == ["src/tandem/chat/window.py"]
    assert match("zzz", PATHS, 5) == []


def test_limit_caps_the_result():
    assert len(match("s", PATHS, 2)) == 2

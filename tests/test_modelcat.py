"""tandem-model header parsing and catalog resolution."""

import json

import pytest

from tandem import modelcat, paths

CATALOG = [
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "visibility": "list"},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "visibility": "list"},
    {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini", "visibility": "list"},
    {"slug": "codex-auto-review", "display_name": "Codex Auto Review",
     "visibility": "hide"},
]


class TestSplitModelHeader:
    def test_header_is_stripped_and_returned(self):
        got = modelcat.split_model_header("tandem-model: gpt-5.6-sol\nreview x")
        assert got == ("gpt-5.6-sol", "review x")

    def test_no_header_passes_through(self):
        assert modelcat.split_model_header("review x\nline 2") == ("", "review x\nline 2")

    def test_header_requires_first_line(self):
        task = "review x\ntandem-model: gpt-5.6-sol"
        assert modelcat.split_model_header(task) == ("", task)

    def test_malformed_name_left_in_place(self):
        # trailing prose after the name is not a header
        task = "tandem-model: gpt-5.6-sol please\nreview x"
        assert modelcat.split_model_header(task) == ("", task)

    def test_overlong_name_left_in_place(self):
        task = f"tandem-model: {'x' * 65}\nreview x"
        assert modelcat.split_model_header(task) == ("", task)

    def test_header_only_brief_yields_empty_task(self):
        assert modelcat.split_model_header("tandem-model: gpt-5.6-sol") == \
            ("gpt-5.6-sol", "")

    def test_no_space_after_colon_still_parses(self):
        assert modelcat.split_model_header("tandem-model:sol\nt") == ("sol", "t")


class TestResolve:
    def test_exact_slug(self):
        assert modelcat.resolve("gpt-5.6-sol", CATALOG) == "gpt-5.6-sol"

    def test_display_name_case_and_punctuation_insensitive(self):
        assert modelcat.resolve("GPT 5.6 Sol", CATALOG) == "gpt-5.6-sol"

    def test_unique_substring(self):
        assert modelcat.resolve("sol", CATALOG) == "gpt-5.6-sol"
        assert modelcat.resolve("5.4 mini", CATALOG) == "gpt-5.4-mini"

    def test_ambiguous_lists_visible_slugs(self):
        with pytest.raises(modelcat.UnknownModel) as e:
            modelcat.resolve("gpt", CATALOG)
        msg = str(e.value)
        assert "unknown model 'gpt'" in msg
        assert "gpt-5.6-sol" in msg and "gpt-5.4-mini" in msg
        assert "codex-auto-review" not in msg

    def test_no_match_lists_visible_slugs(self):
        with pytest.raises(modelcat.UnknownModel) as e:
            modelcat.resolve("o3", CATALOG)
        assert "unknown model 'o3'; this codex offers: gpt-5.6-sol" in str(e.value)

    def test_hidden_models_never_match(self):
        with pytest.raises(modelcat.UnknownModel):
            modelcat.resolve("auto review", CATALOG)

    def test_none_catalog_passes_name_through(self):
        assert modelcat.resolve("anything-goes", None) == "anything-goes"


class TestLoadCatalog:
    def test_reads_models_array(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text(
            json.dumps({"fetched_at": "t", "models": CATALOG}))
        assert modelcat.load_catalog() == CATALOG

    def test_missing_file_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert modelcat.load_catalog() is None

    def test_broken_json_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text("{nope")
        assert modelcat.load_catalog() is None

    def test_models_not_a_list_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text(json.dumps({"models": 7}))
        assert modelcat.load_catalog() is None


def test_model_footer_exact_text():
    assert modelcat.model_footer("gpt-5.6-sol") == "[tandem-sub model: gpt-5.6-sol]"

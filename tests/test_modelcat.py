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

    def test_header_only_brief_yields_empty_task(self):
        assert modelcat.split_model_header("tandem-model: gpt-5.6-sol") == \
            ("gpt-5.6-sol", "")

    def test_no_space_after_colon_still_parses(self):
        assert modelcat.split_model_header("tandem-model:sol\nt") == ("sol", "t")

    # --- near-misses that used to fall through as "no header" (live-probed) ---

    def test_trailing_space_still_parses(self):
        assert modelcat.split_model_header("tandem-model: gpt-5.6-sol  \nreview x") \
            == ("gpt-5.6-sol", "review x")

    def test_trailing_tab_still_parses(self):
        assert modelcat.split_model_header("tandem-model: gpt-5.6-sol\t\nreview x") \
            == ("gpt-5.6-sol", "review x")

    def test_crlf_line_ending_still_parses(self):
        assert modelcat.split_model_header("tandem-model: gpt-5.6-sol\r\nreview x") \
            == ("gpt-5.6-sol", "review x")

    def test_mixed_case_prefix_still_parses(self):
        assert modelcat.split_model_header("Tandem-Model: gpt-5.6-sol\nreview x") \
            == ("gpt-5.6-sol", "review x")
        assert modelcat.split_model_header("TANDEM-MODEL:gpt-5.6-sol\nreview x") \
            == ("gpt-5.6-sol", "review x")

    def test_spoken_multi_word_name_parses_and_resolves(self):
        # internal spaces are legal; resolve() normalizes them away
        name, rest = modelcat.split_model_header("tandem-model: 5.4 mini\nreview x")
        assert (name, rest) == ("5.4 mini", "review x")
        assert modelcat.resolve(name, CATALOG) == "gpt-5.4-mini"

    def test_prose_after_the_name_parses_and_fails_loudly_at_resolve(self):
        # with internal spaces legal this is one (unresolvable) name — the
        # failure is loud at resolve() instead of a silent pass-through
        name, rest = modelcat.split_model_header(
            "tandem-model: gpt-5.6-sol please\nreview x")
        assert (name, rest) == ("gpt-5.6-sol please", "review x")
        with pytest.raises(modelcat.UnknownModel):
            modelcat.resolve(name, CATALOG)

    # --- near-misses that are LOUD errors, never silent pass-through ---

    def test_decorated_name_is_malformed(self):
        with pytest.raises(modelcat.MalformedHeader) as e:
            modelcat.split_model_header("tandem-model: <gpt-5.4-mini>\nreview x")
        assert str(e.value) == \
            "malformed tandem-model header: tandem-model: <gpt-5.4-mini>"

    def test_overlong_name_is_malformed(self):
        with pytest.raises(modelcat.MalformedHeader):
            modelcat.split_model_header(f"tandem-model: {'x' * 65}\nreview x")

    def test_empty_name_is_malformed(self):
        with pytest.raises(modelcat.MalformedHeader):
            modelcat.split_model_header("tandem-model:\nreview x")
        with pytest.raises(modelcat.MalformedHeader):
            modelcat.split_model_header("tandem-model:   \nreview x")

    def test_prose_that_opens_with_the_prefix_is_a_loud_error_by_design(self):
        # A brief whose first line talks *about* the protocol is rare; a brief
        # whose first line is a near-miss header is not. Pinned decision: any
        # first line starting with the prefix must resolve or fail loudly, so
        # this errors rather than shipping the line to codex as task text.
        with pytest.raises(modelcat.MalformedHeader):
            modelcat.split_model_header(
                "tandem-model: is a protocol, not a topic\nexplain it")

    def test_malformed_header_is_a_value_error(self):
        assert issubclass(modelcat.MalformedHeader, ValueError)

    def test_line_not_starting_with_the_prefix_is_untouched_task_text(self):
        task = "the tandem-model: header is documented below\nmore"
        assert modelcat.split_model_header(task) == ("", task)


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

    def test_empty_models_array_is_none(self, tmp_path, monkeypatch):
        # an empty catalog is as unusable as a missing one: [] would make
        # every dispatch fail with "this codex offers: " and nothing after it
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text(json.dumps({"models": []}))
        assert modelcat.load_catalog() is None

    def test_models_without_objects_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text(json.dumps({"models": ["junk"]}))
        assert modelcat.load_catalog() is None


def test_model_footer_exact_text():
    assert modelcat.model_footer("gpt-5.6-sol") == "[tandem-sub model: gpt-5.6-sol]"

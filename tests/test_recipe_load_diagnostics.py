from unittest.mock import patch

from pt503_tester.recipe_store import RecipeStore


def test_recipe_load_failure_logs_without_overwriting_corrupt_file(tmp_path, caplog):
    path = tmp_path / "recipes.json"
    original = b'{"recipes":'
    path.write_bytes(original)
    with caplog.at_level("WARNING", logger="pt503_tester.recipe_store"):
        store = RecipeStore(path)
    assert store.get_recipe().name == "Default"
    assert store.get_recipe().index == 1
    assert path.read_bytes() == original
    assert len(caplog.records) == 1
    assert str(path) in caplog.text
    assert "using in-memory Default recipe" in caplog.text


def test_recipe_read_error_logs_and_retains_existing_fallback(tmp_path, caplog):
    path = tmp_path / "recipes.json"
    RecipeStore(path)
    original = path.read_bytes()
    with patch.object(type(path), "read_bytes", side_effect=OSError("read denied")):
        with caplog.at_level("WARNING", logger="pt503_tester.recipe_store"):
            store = RecipeStore(path)
    assert store.get_recipe().name == "Default"
    assert path.read_bytes() == original
    assert len(caplog.records) == 1
    assert "read denied" in caplog.text

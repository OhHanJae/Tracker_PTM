import json

import pytest

from pt503_tester.drawing_store import DrawingStore
from test_drawing_recipes import fvp_archive, stores


def test_names_and_order_survive_edits_restart_and_zip_restore(tmp_path):
    drawings, recipes, first, _ = stores(tmp_path)
    second = drawings.create_from_uploads([('second.zip', fvp_archive()[0])])['id']
    drawings.reorder_drawings([second, first])
    drawings.update_drawing(first, '사용자 도면 이름')
    assert [item['id'] for item in drawings.list_drawings()] == [second, first]
    restored = DrawingStore(drawings.root, recipe_store=recipes)
    assert [item['id'] for item in restored.list_drawings()] == [second, first]
    assert restored.get_drawing(first)['name'] == '사용자 도면 이름'
    copy = restored.create_from_uploads([('backup.zip', restored.archive(first))])
    assert copy['name'] == '사용자 도면 이름'
    assert copy['order'] == 3
    before = json.loads(restored.manifest_path.read_text(encoding='utf-8'))
    with pytest.raises(ValueError):
        restored.reorder_drawings([first, first])
    with pytest.raises(ValueError):
        restored.update_drawing(first, ' ')
    assert json.loads(restored.manifest_path.read_text(encoding='utf-8')) == before


def test_seed_deletion_does_not_reappear_on_restart(tmp_path):
    uploaded, recipes, drawing_id, _ = stores(tmp_path)
    package_root = tmp_path / 'packages'
    package = package_root / 'Seed'
    (package / 'model').mkdir(parents=True)
    (package / 'coordinates').mkdir()
    assets = uploaded.root / drawing_id
    (package / 'model' / 'product.glb').write_bytes((assets / 'product.glb').read_bytes())
    for name in ('coordinates_manifest.json', 'group_1.json', 'group_2.json', 'group_3.json'):
        (package / 'coordinates' / name).write_bytes((assets / name).read_bytes())
    drawings = DrawingStore(tmp_path / 'seed_state', package_root, recipe_store=recipes)
    seed_id = drawings.list_drawings()[0]['id']
    assert drawings.get_drawing(seed_id)['deletable']
    drawings.delete_drawing(seed_id)
    assert DrawingStore(drawings.root, package_root, recipe_store=recipes).list_drawings() == []
    assert (package / 'model' / 'product.glb').is_file()

import json
from unittest.mock import Mock, patch

import pytest

from pt503_tester.headless_server import HeadlessController, HeadlessApiError
from pt503_tester.recipe_store import RecipeStore, RecipeStorageError


@pytest.fixture
def controller(tmp_path):
    output = Mock(connected=True)
    output.write.side_effect = bool
    with patch('pt503_tester.headless_server.GpioLaser', return_value=output):
        c = HeadlessController(RecipeStore(tmp_path / 'recipes.json'), settings_path=tmp_path / 'settings.json')
    c.auto_reconnect_config['enabled'] = False
    c.send = Mock(return_value=[])
    try:
        yield c
    finally:
        c.shutdown()


def test_corrupt_store_cannot_move_or_overwrite_file(tmp_path, controller):
    path = tmp_path / 'bad.json'
    original = b'{"recipes":'
    path.write_bytes(original)
    store = RecipeStore(path)
    assert store.storage_error
    with pytest.raises(RecipeStorageError):
        store.upsert_recipe(name='must not overwrite')
    controller.recipe_store = store
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('point.goto', {'recipe_id': 1, 'point_id': 1})
    assert exc.value.code == 'RECIPE_STORAGE_ERROR'
    assert path.read_bytes() == original


def test_second_store_cannot_overwrite_newer_revision(tmp_path):
    path = tmp_path / 'shared.json'
    first = RecipeStore(path)
    second = RecipeStore(path)
    first.upsert_recipe(name='first writer')
    original = path.read_bytes()
    with pytest.raises(RecipeStorageError, match='changed externally'):
        second.upsert_recipe(name='stale writer')
    assert path.read_bytes() == original
    second.load()
    second.upsert_recipe(name='after reload')
    assert len(RecipeStore(path).list_recipes()) == 3


def test_invalid_schema_and_duplicate_indices_are_not_production_data(tmp_path):
    path = tmp_path / 'recipes.json'
    store = RecipeStore(path)
    data = store.export_document()
    data['recipes'].append({**data['recipes'][0], 'id': 'different-id'})
    path.write_text(json.dumps(data), encoding='utf-8')
    loaded = RecipeStore(path)
    assert 'duplicate recipe index' in loaded.storage_error
    with pytest.raises(RecipeStorageError):
        loaded.save()


def test_import_index_collision_requires_explicit_remapping(tmp_path):
    first = RecipeStore(tmp_path / 'first.json')
    second = RecipeStore(tmp_path / 'second.json')
    before = second.path.read_bytes()
    with pytest.raises(ValueError, match='explicit remapping'):
        second.import_document(first.export_document())
    assert second.path.read_bytes() == before


def test_automatic_move_requires_home_and_blocks_competing_jog_and_edits(controller):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='P1', pan=10, tilt=5)
    params = {'recipe_id': recipe.id, 'point_id': point.id, 'require_homed': True, 'laser_after_move': False}
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('point.goto', params)
    assert exc.value.code == 'HOMING_REQUIRED'
    controller._home_valid = True
    response = controller.command('point.goto', params)
    assert controller.status()['motion']['target_motion_id'] == response['motion_id']
    assert controller.status()['motion']['target_position'] == {'pan': 10, 'tilt': 5}
    for command, args in [('motion.jog', {'pan': 'right'}), ('point.delete', {'recipe_id': recipe.id, 'point_id': point.id})]:
        with pytest.raises(HeadlessApiError) as exc:
            controller.command(command, args)
        assert exc.value.code == 'BUSY'
    controller.command('motion.stop', {})
    assert controller.status()['motion']['target_motion_id'] is None


def test_recipe_change_before_completion_cannot_report_success_or_relight(controller):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='P1', pan=10, tilt=5)
    controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id, 'laser_after_move': False})
    controller.recipe_store.upsert_point(recipe.id, point_id=point.id, name='changed', pan=20, tilt=5)
    controller._finish_pending_motion()
    assert controller.status()['motion']['last_error'] == 'CONFIG_OR_CAL_ERROR'
    assert controller.status()['motion']['target_motion_id'] is None


def test_position_freshness_is_per_axis_and_cleared_on_disconnect(controller):
    body = bytes([controller._address, 0, 0x59, 0, 10])
    controller._decode_blob(b'\xff' + body + bytes([sum(body) & 255]))
    state = controller.status()
    assert state['position_age_ms']['pan'] is not None
    assert state['position_age_ms']['tilt'] is None
    controller.close()
    assert controller.status()['position_age_ms'] == {'pan': None, 'tilt': None}


def test_local_priority_does_not_override_plc_off_or_reserved_mode(controller):
    controller.command('laser.settings', {'control_priority': True})
    for mode in (0, 6, 65535):
        controller.command('laser.power_save', {'plc_mode': mode, 'enabled': False, 'manual_allowed': False,
                                               'state_allowed': False, 'local_allowed': True})
        with pytest.raises(HeadlessApiError):
            controller.command('laser.on', {})
        assert controller.laser_on is False


def test_pinned_recipe_revision_rejects_changed_numeric_mapping(controller):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='P1', pan=10, tilt=5)
    revision = controller.recipe_store.recipe_revision(recipe.id)
    controller.recipe_store.upsert_point(recipe.id, point_id=point.id, name='new target', pan=20, tilt=5)
    controller._home_valid = True
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id, 'require_homed': True,
                                          'expected_recipe_revision': revision})
    assert exc.value.code == 'CONFIG_OR_CAL_ERROR'
    assert controller._pending_motion is None

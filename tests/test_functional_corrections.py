import json
import time
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


@pytest.mark.parametrize('command,params', [
    ('motion.jog', {'pan': 'right'}),
    ('point.goto', {}),
    ('home.start', {}),
    ('point.delete', {}),
    ('recipe.select', {}),
])
def test_web_point_move_rejects_competing_commands_until_stop(controller, command, params):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='test', pan=10, tilt=5)
    moved = controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id, 'laser_after_move': False, 'exclusive': True})
    before = controller.send.call_count
    with pytest.raises(HeadlessApiError) as exc:
        controller.command(command, params)
    assert exc.value.code == 'BUSY'
    assert controller.send.call_count == before
    motion = controller.status()['motion']
    assert motion['id'] == moved['motion_id']
    assert motion['command'] == 'point.goto' and motion['automatic'] is False
    controller.command('motion.stop', {})
    assert controller.status()['motion']['failed_id'] == moved['motion_id']
    controller.command('motion.jog', {'pan': 'right'})
    assert controller.motion_state == 'jog'


def completed_test_move(controller):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='test', pan=10, tilt=5)
    moved = controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id, 'laser_after_move': False})
    controller.motion_state = 'completed'
    controller._finish_pending_motion()
    return {
        'recipe_id': recipe.id, 'point_id': point.id, 'duration_ms': 1000,
        'expected_motion_id': moved['motion_id'],
        'expected_recipe_revision': moved['recipe_revision'],
        'laser_permit_token': moved['laser_permit_token'],
    }


def test_recipe_test_pulse_accepts_only_the_completed_target(controller):
    params = completed_test_move(controller)
    assert controller.command('laser.pulse', params)['laser_on'] is True
    controller.command('laser.off', {})
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('laser.pulse', params)
    assert exc.value.code == 'LASER_INTERLOCK'
    assert controller.laser_on is False


@pytest.mark.parametrize('command', ['laser.on', 'laser.pulse'])
def test_stale_test_pulse_cannot_replace_new_manual_output(controller, command):
    params = completed_test_move(controller)
    controller.command(command, {} if command == 'laser.on' else {'duration_ms': 5000})
    deadline = controller._laser_manual_deadline
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('laser.pulse', params)
    assert exc.value.code == 'LASER_INTERLOCK'
    assert controller.laser_on is True
    assert controller._laser_manual_deadline == deadline


@pytest.mark.parametrize('command', ['recipe.pose_preview', 'recipe.export', 'motion.completion_config', 'motion.tolerance'])
def test_test_move_allows_read_only_queries(controller, command):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='test', pan=10, tilt=5)
    moved = controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id,
        'laser_after_move': False, 'exclusive': True})
    response = controller.command(command, {'recipe_id': recipe.id} if command.startswith('recipe.') else {})
    assert response
    assert controller.status()['motion']['id'] == moved['motion_id']


def test_delayed_test_move_is_rejected_after_stop_and_off(controller):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='test', pan=10, tilt=5)
    before_stop = controller.command('laser.off', {})
    controller.command('motion.stop', {})
    controller.command('laser.off', {})
    controller.send.reset_mock()
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id,
            'exclusive': True, 'laser_after_move': False, 'laser_permit_token': before_stop['permit_token']})
    assert exc.value.code == 'LASER_INTERLOCK'
    assert controller._pending_motion is None
    controller.send.assert_not_called()


def test_idle_stop_alone_cancels_prepared_test_move(controller):
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='test', pan=10, tilt=5)
    permit = controller.command('laser.off', {})['permit_token']
    controller.command('motion.stop', {'preserve_laser': True})
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('point.goto', {'recipe_id': recipe.id, 'point_id': point.id,
            'exclusive': True, 'laser_after_move': False, 'laser_permit_token': permit})
    assert exc.value.code == 'LASER_INTERLOCK'
    assert controller._pending_motion is None


@pytest.mark.parametrize('motion', ['point.goto', 'motion.jog'])
def test_plc_preparation_cannot_extinguish_another_motion_output(controller, motion):
    controller.command('laser.on', {})
    recipe = controller.recipe_store.get_recipe()
    point = controller.recipe_store.upsert_point(recipe.id, name='test', pan=10, tilt=5)
    controller.command(motion, {'recipe_id': recipe.id, 'point_id': point.id, 'exclusive': True}
        if motion == 'point.goto' else {'pan': 'right'})
    generation = controller._laser_generation
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('laser.off', {'require_idle': True})
    assert exc.value.code == 'BUSY'
    assert controller.laser_on is True
    assert controller._laser_generation == generation
    controller.command('motion.stop', {})
    assert controller.command('laser.off', {'require_idle': True})['laser_on'] is False


@pytest.mark.parametrize('change', ['stop', 'jog', 'disconnect', 'recipe', 'selection'])
def test_stale_test_pulse_cannot_relight_after_external_change(controller, change):
    params = completed_test_move(controller)
    if change == 'stop':
        controller.command('motion.stop', {})
    elif change == 'jog':
        controller.command('motion.jog', {'pan': 'right'})
    elif change == 'disconnect':
        controller.close()
    elif change == 'recipe':
        controller.recipe_store.upsert_point(params['recipe_id'], point_id=params['point_id'], name='edited', pan=20, tilt=5)
    else:
        other = controller.recipe_store.upsert_recipe(name='other')
        controller.command('recipe.select', {'recipe_id': other.id})
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('laser.pulse', params)
    assert exc.value.code in {'STALE_TARGET', 'CONFIG_OR_CAL_ERROR', 'LASER_INTERLOCK'}
    assert controller.laser_on is False


def _start_recipe_test(controller):
    controller._serial = Mock(is_open=True, in_waiting=0)
    unavailable = HeadlessApiError('CORE_TEST_GUARD_FAILED', 'standalone test fixture')
    unavailable.__cause__ = ConnectionRefusedError()
    controller._core_request = Mock(side_effect=unavailable)
    recipe = controller.recipe_store.get_recipe()
    points = [controller.recipe_store.upsert_point(recipe.id, name=f'P{i}', pan=10*i, tilt=5*i)
              for i in (1, 2)]
    session = controller.command('test.start', {'mode': 'sequence', 'recipe_id': recipe.id})
    return recipe, points, session['test_token']


@pytest.mark.parametrize('command,params', [
    ('motion.jog', {'pan': 'right'}), ('home.start', {}), ('motion.absolute', {'pan': 10}),
    ('point.goto', {'recipe_id': 1, 'point_id': 1, 'require_homed': True}),
    ('laser.on', {}), ('laser.pulse', {'duration_ms': 1000}),
    ('laser.off', {'require_idle': True}), ('recipe.upsert', {'name': 'other'}),
    ('raw.send', {'hex': 'FF 01 00 00 00 00 01'}),
])
def test_full_test_session_blocks_plc_and_manual_during_point_gaps(controller, command, params):
    recipe, points, token = _start_recipe_test(controller)
    assert controller.status()['operation']['mode'] == 'testing'
    before = controller._laser_generation
    with pytest.raises(HeadlessApiError) as exc:
        controller.command(command, params)
    assert exc.value.code == 'BUSY'
    assert controller._laser_generation == before
    assert controller.status()['operation']['test_active']
    controller.command('test.renew', {'test_token': token})
    for point in points:
        off = controller.command('laser.off', {'test_token': token})
        move = controller.command('point.goto', {'test_token': token, 'recipe_id': recipe.id,
            'point_id': point.id, 'laser_after_move': False, 'exclusive': True,
            'laser_permit_token': off['permit_token']})
        controller.motion_state = 'completed'
        controller._finish_pending_motion()
        pulse = controller.command('laser.pulse', {'test_token': token, 'duration_ms': 1000,
            'recipe_id': recipe.id, 'point_id': point.id, 'expected_motion_id': move['motion_id'],
            'expected_recipe_revision': move['recipe_revision'], 'laser_permit_token': move['laser_permit_token']})
        assert pulse['laser_on']
        with pytest.raises(HeadlessApiError) as exc:
            controller.command('motion.jog', {'pan': 'right'})
        assert exc.value.code == 'BUSY' and controller.laser_on
        controller.command('laser.off', {'test_token': token})
        assert controller.status()['operation']['test_active']
    assert controller.command('test.stop', {'test_token': token}) == {
        'accepted': True, 'stopped': True, 'laser_on': False}
    assert controller.status()['operation']['mode'] == 'idle'


@pytest.mark.parametrize('command', ['motion.stop', 'motion.force_stop', 'laser.off', 'serial.disconnect'])
def test_global_stop_and_disconnect_cancel_test(controller, command):
    _, _, token = _start_recipe_test(controller)
    controller.command(command, {})
    assert not controller.status()['operation']['test_active']
    assert not controller.laser_on
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('point.goto', {'test_token': token})
    assert exc.value.code == 'TEST_SESSION_EXPIRED'


def test_test_lease_expiry_stops_motion_and_stale_cleanup_cannot_stop_new_test(controller):
    recipe, points, token = _start_recipe_test(controller)
    moved = controller.command('point.goto', {'test_token': token, 'recipe_id': recipe.id,
        'point_id': points[0].id, 'laser_after_move': False})
    controller._test_session['expires_at'] = time.monotonic() - 1
    status = controller.status()
    assert not status['operation']['test_active']
    assert status['motion']['failed_id'] == moved['motion_id']
    assert status['motion']['state'] == 'stopped' and not controller.laser_on
    new = controller.command('test.start', {'mode': 'random', 'recipe_id': recipe.id})
    assert new['test_token'] != token
    assert controller.command('test.stop', {'test_token': token})['accepted'] is False
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('test.renew', {'test_token': token})
    assert exc.value.code == 'TEST_SESSION_EXPIRED'
    assert controller.status()['operation']['test_active']


@pytest.mark.parametrize('mode', [2, 3, 4, 5])
def test_plc_operating_laser_modes_cannot_take_over_test(controller, mode):
    _, _, token = _start_recipe_test(controller)
    controller.command('laser.pulse', {'test_token': token, 'duration_ms': 1000})
    controller.command('laser.power_save', {'plc_mode': mode, 'enabled': True, 'automatic_off': True,
        'manual_allowed': False, 'state_allowed': False, 'local_allowed': True})
    assert controller._effective_laser_mode() == 1
    assert controller.laser_on and controller.status()['operation']['test_active']


@pytest.mark.parametrize('mode,allowed', [(0, True), (6, True), (1, False)])
def test_plc_off_and_real_interlock_cancel_test(controller, mode, allowed):
    _, _, token = _start_recipe_test(controller)
    controller.command('laser.pulse', {'test_token': token, 'duration_ms': 1000})
    controller.command('laser.power_save', {'plc_mode': mode, 'enabled': False, 'automatic_off': True,
        'manual_allowed': False, 'state_allowed': False, 'local_allowed': allowed})
    assert not controller.laser_on
    assert not controller.status()['operation']['test_active']


@pytest.mark.parametrize('command,params', [
    ('motion.jog', {'pan': 'right'}), ('motion.absolute', {'pan': 10}),
    ('home.start', {}), ('laser.on', {}),
])
def test_server_reports_manual_operation_until_stop(controller, command, params):
    assert controller.status()['operation']['mode'] == 'idle'
    controller.command(command, params)
    assert controller.status()['operation']['mode'] == 'manual'
    controller.command('motion.force_stop', {})
    assert controller.status()['operation']['mode'] == 'idle'


def test_automatic_home_is_not_reported_as_manual(controller):
    controller.command('home.start', {'automatic': True})
    assert controller.status()['operation']['manual_active'] is False


def test_pagehide_from_previous_manual_page_does_not_cancel_active_test(controller):
    _, _, token = _start_recipe_test(controller)
    for command in ('motion.stop', 'laser.off'):
        result = controller.command(command, {'if_no_test_session': True})
        assert result['accepted'] is False
        assert controller._test_session['token'] == token
    controller.command('motion.stop', {})
    assert not controller.status()['operation']['test_active']


def test_core_reservation_prevents_start_and_releases_after_test_stop(controller):
    recipe, _, token = _start_recipe_test(controller)
    controller.command('test.stop', {'test_token': token})
    controller._core_request = Mock(return_value={'accepted': False})
    with pytest.raises(HeadlessApiError) as exc:
        controller.command('test.start', {'mode': 'sequence', 'recipe_id': recipe.id})
    assert exc.value.code == 'BUSY' and controller._test_session is None
    controller._core_request = Mock(return_value={'accepted': True})
    started = controller.command('test.start', {'mode': 'sequence', 'recipe_id': recipe.id})
    assert controller._test_session['core_guard'] is True
    controller.command('test.renew', {'test_token': started['test_token']})
    controller.command('test.stop', {'test_token': started['test_token']})
    assert [call.args[0] for call in controller._core_request.call_args_list] == [
        'operation.test.begin', 'operation.test.renew', 'operation.test.end']


def test_managed_test_does_not_extend_lease_when_core_renewal_fails(controller):
    recipe, _, token = _start_recipe_test(controller)
    controller.command('test.stop', {'test_token': token})
    controller._core_request = Mock(return_value={'accepted': True})
    started = controller.command('test.start', {'mode': 'sequence', 'recipe_id': recipe.id})
    before = controller._test_session['expires_at']
    controller._core_request.side_effect = HeadlessApiError('CORE_TEST_GUARD_FAILED', 'connection lost')
    with pytest.raises(HeadlessApiError):
        controller.command('test.renew', {'test_token': started['test_token']})
    assert controller._test_session['expires_at'] == before


def test_session_api_is_advertised_only_by_the_headless_runtime(controller):
    from pt503_tester.api_metadata import supported_command_names
    commands = controller.command('system.commands', {})['commands']
    for command in ('test.start', 'test.renew', 'test.stop'):
        assert command in commands
        assert command not in supported_command_names()

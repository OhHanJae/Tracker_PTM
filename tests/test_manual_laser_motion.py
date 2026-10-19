import copy
from unittest.mock import Mock, patch

import pytest

from pt503_tester.headless_server import HeadlessController
from pt503_tester.recipe_store import RecipeStore


@pytest.fixture
def controller(tmp_path):
    output = Mock(connected=True)
    output.write.side_effect = bool
    with patch('pt503_tester.headless_server.GpioLaser', return_value=output):
        control = HeadlessController(
            RecipeStore(tmp_path / 'recipes.json'),
            settings_path=tmp_path / 'settings.json',
        )
    control.auto_reconnect_config['enabled'] = False
    control.send = Mock(return_value=[])
    try:
        yield control, output
    finally:
        control.shutdown()


@pytest.mark.parametrize('command,params', [
    ('motion.jog', {'pan': 'right', 'tilt': 'up', 'speed_level': 5}),
    ('motion.absolute', {'pan': 10, 'tilt': 5}),
    ('motion.relative', {'pan_delta': 1}),
    ('motion.goto', {}),
    ('point.goto', {}),
    ('home.start', {}),
])
@pytest.mark.parametrize('blink', [False, True])
def test_manual_motion_preserves_laser_output_and_override(controller, command, params, blink):
    control, output = controller
    recipe = control.recipe_store.get_recipe()
    point = control.recipe_store.upsert_point(recipe.id, name='target', pan=10, tilt=5)
    control._selected_recipe_id, control._selected_point_id = recipe.id, point.id
    if blink:
        profiles = copy.deepcopy(control.laser_state_profiles)
        profiles['wrong_point'] = {'mode': 'blink', 'interval_ms': 100}
        control.command('laser.settings', {'state_profiles': profiles})
        control.command('laser.state', {'state': 'wrong_point', 'manual': True})
    else:
        control.command('laser.on', {})

    with control._lock, control._laser_lock:
        before = (control._laser_manual_deadline, control._laser_generation,
                  control._laser_active_state, control._laser_blink_next)
        output.write.reset_mock()
        control.command(command, params)
        # Direction/speed changes and releasing the stick must also keep the state.
        if command == 'motion.jog':
            control.command('motion.jog', {'pan': 'left', 'speed_level': 3})
            control.command('motion.jog', {'pan': 'left', 'speed_level': 3})
            control.command('motion.jog', {'pan': 'stop', 'tilt': 'stop'})
        control.command('motion.stop', {'preserve_laser_manual': True})
        assert control.laser_on
        assert (control._laser_manual_deadline, control._laser_generation,
                control._laser_active_state, control._laser_blink_next) == before
        output.write.assert_not_called()


def test_manual_point_completion_does_not_replace_output_or_relight_after_off(controller):
    control, output = controller
    recipe = control.recipe_store.get_recipe()
    point = control.recipe_store.upsert_point(recipe.id, name='target', pan=10, tilt=5)
    params = {'recipe_id': recipe.id, 'point_id': point.id}
    for turn_off in (False, True):
        control.command('laser.on', {})
        control.command('point.goto', params)
        assert control.laser_on
        if turn_off:
            control.command('laser.off', {})
        output.write.reset_mock()
        control._finish_pending_motion()
        assert control.laser_on is (not turn_off)
        output.write.assert_not_called()


@pytest.mark.parametrize('command,params', [
    ('motion.jog', {'pan': 'right', 'speed_level': 5}),
    ('motion.absolute', {'pan': 10}),
    ('motion.relative', {'pan_delta': 1}),
])
def test_manual_motion_keeps_laser_off(controller, command, params):
    control, output = controller
    control.command('laser.off', {})
    output.write.reset_mock()
    control.command(command, params)
    control.command('motion.stop', {'preserve_laser_manual': True})
    assert not control.laser_on
    output.write.assert_not_called()


@pytest.mark.parametrize('command,params', [
    ('motion.stop', {}),
    ('motion.force_stop', {'preserve_laser_manual': True}),
    ('laser.off', {}),
    ('laser.settings', {'bypass': True}),
    ('laser.power_save', {'enabled': True, 'manual_allowed': False, 'state_allowed': False}),
])
def test_safety_commands_still_turn_manual_laser_off(controller, command, params):
    control, _ = controller
    control.command('laser.on', {})
    control.command('motion.jog', {'pan': 'right', 'speed_level': 5})
    control.command(command, params)
    assert not control.laser_on
    assert not control._laser_manual_deadline


def test_manual_jog_still_cancels_automatic_laser_state(controller):
    control, _ = controller
    control.command('laser.state', {'state': 'point_irradiation'})
    control.command('motion.jog', {'pan': 'right', 'speed_level': 5})
    assert not control.laser_on
    assert control._laser_active_state is None

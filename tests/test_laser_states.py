import copy
import time
from unittest.mock import Mock, patch

import pytest

from pt503_tester.headless_server import HeadlessApiError, HeadlessController
from pt503_tester.recipe_store import RecipeStore


@pytest.fixture
def controller(tmp_path):
    output = Mock(connected=True)
    levels = []
    output.write.side_effect = lambda value: levels.append(bool(value)) or bool(value)
    with patch('pt503_tester.headless_server.GpioLaser', return_value=output):
        control = HeadlessController(RecipeStore(tmp_path / 'recipes.json'), settings_path=tmp_path / 'settings.json')
    control.auto_reconnect_config['enabled'] = False
    control.send = Mock(return_value=[])
    control.test_levels = levels
    try:
        yield control
    finally:
        control.shutdown()


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def set_blink(control, state='wrong_point'):
    profiles = copy.deepcopy(control.laser_state_profiles)
    profiles[state] = {'mode': 'blink', 'interval_ms': 100}
    control.command('laser.settings', {'state_profiles': profiles})


def test_profiles_save_without_firing_and_invalid_settings_are_atomic(controller):
    control = controller
    set_blink(control)
    assert not any(control.test_levels)
    saved = control._settings_path.read_bytes()
    profiles = copy.deepcopy(control.laser_state_profiles)
    profiles['wrong_point']['interval_ms'] = 0
    with pytest.raises(ValueError):
        control.command('laser.settings', {'state_profiles': profiles})
    assert control._settings_path.read_bytes() == saved
    control._load_settings()
    assert control.laser_state_profiles['wrong_point']['interval_ms'] == 100
    profiles['wrong_point']['interval_ms'] = 200
    previous = copy.deepcopy(control.laser_state_profiles)
    with patch.object(control, '_save_settings', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            control.command('laser.settings', {'state_profiles': profiles})
    assert control.laser_state_profiles == previous


def test_blink_keeps_manual_lease_and_off_cancels_future_high(controller):
    control = controller
    set_blink(control)
    control.command('laser.state', {'state': 'wrong_point', 'manual': True})
    generation = control._laser_generation
    lease = control._laser_manual_deadline
    wait_until(lambda: len(control.test_levels) >= 4)
    assert control._laser_generation == generation
    assert control._laser_manual_deadline == lease
    control.command('laser.off', {})
    count = len(control.test_levels)
    time.sleep(0.2)
    assert control.test_levels[-1] is False
    assert len(control.test_levels) == count
    assert control.status()['laser']['active_state'] is None


@pytest.mark.parametrize('stop_command,params', [
    ('motion.force_stop', {}), ('motion.stop', {}),
    ('laser.settings', {'bypass': True}),
    ('laser.power_save', {'enabled': True, 'manual_allowed': False, 'state_allowed': False}),
])
def test_stop_bypass_and_interlock_cancel_blink(controller, stop_command, params):
    set_blink(controller)
    controller.command('laser.state', {'state': 'wrong_point', 'manual': True})
    controller.command(stop_command, params)
    time.sleep(0.15)
    assert controller.test_levels[-1] is False
    assert controller._laser_active_state is None
    assert not controller._laser_blink_next


def test_completed_signal_uses_separate_permission_and_manual_remains_blocked(controller):
    control = controller
    set_blink(control, 'work_complete')
    control.command('laser.power_save', {'enabled': False, 'automatic_off': True,
                                        'manual_allowed': False, 'state_allowed': True})
    control.command('laser.state', {'state': 'work_complete'})
    assert control.test_levels[-1] is True
    with pytest.raises(HeadlessApiError):
        control.command('laser.on', {})
    control.command('laser.power_save', {'enabled': False, 'manual_allowed': False, 'state_allowed': False})
    assert control.test_levels[-1] is False
    assert control._laser_active_state is None


def test_manual_expiry_and_motion_disallow_emission(controller):
    control = controller
    set_blink(control)
    control.command('laser.state', {'state': 'wrong_point', 'manual': True})
    with control._laser_lock:
        control._laser_manual_deadline = time.monotonic() - 1
    wait_until(lambda: control._laser_active_state is None)
    assert control.test_levels[-1] is False
    control._pending_motion = {'motion_id': 'moving'}
    with pytest.raises(HeadlessApiError) as error:
        control.command('laser.state', {'state': 'point_irradiation', 'manual': True})
    assert error.value.code == 'MOTION_ACTIVE'


def test_stale_target_cannot_start_state(controller):
    control = controller
    recipe = control.recipe_store.get_recipe()
    first = control.recipe_store.upsert_point(recipe.id, name='first', pan=1, tilt=2)
    other = control.recipe_store.upsert_point(recipe.id, name='other', pan=3, tilt=4)
    control._selected_recipe_id, control._selected_point_id = recipe.id, first.id
    with pytest.raises(HeadlessApiError) as error:
        control.command('laser.state', {'state': 'wrong_point', 'recipe_id': recipe.id, 'point_id': other.id})
    assert error.value.code == 'STALE_TARGET'
    assert not any(control.test_levels)

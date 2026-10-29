// Browser regressions with simulated PTM responses and gamepad input; no hardware I/O.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const state = {
        serial: {connected: true, port: 'TEST', baudrate: 9600, address: 1},
        position: {pan: 0, tilt: 0}, motion: {state: 'idle'},
        settings: {invert_pan: false, invert_tilt: false, power_on_home: null},
        laser: {on: false, bypass: false, backend: 'gpio', state_profiles: {}},
        last_rx: [], auto_reconnect: {},
    };
    let failStatus = false;
    let testToken = null;
    let testMotion = 0;
    let permitToken = 0;
    let communicationSettings = {timeout_s: 3, status_retries: 2, managed_by: 'Core'};
    let failCommunicationSave = false;
    let recipeList = [];
    let recipePoseBackup = null;
    let failRecipePose = false;
    let drawingList = [{id: 'first', name: 'First'}, {id: 'second', name: 'Second'}];
    const commandNames = ['system.ping', 'serial.scan', 'motion.absolute', 'point.goto', 'laser.on',
        'drawing.select_recipe', 'home.start', 'home.after', 'home.time', 'pelco.flip',
        ...['camera', 'cruise', 'focus', 'lens', 'pattern', 'preset', 'scan', 'zoom',
            'magnification', 'screen', 'zone', 'aux'].map(group => group + '.test')];
    await page.route('http://ptm.test/**', async route => {
        const url = new URL(route.request().url());
        const json = result => route.fulfill({json: {ok: true, result}});
        if (url.pathname === '/api/status')
            return failStatus ? route.fulfill({status: 503, json: {ok: false}}) : json(state);
        if (url.pathname === '/api/command') {
            const {command, params} = route.request().postDataJSON();
            if (command === 'system.commands') return json({commands: commandNames});
            if (command === 'serial.ports') return json({ports: []});
            if (command === 'system.status') return json(state);
            if (command === 'ptm.communication_settings') {
                if ('timeout_s' in params) {
                    if (failCommunicationSave) return route.fulfill({json: {
                        ok: false, error: {message: 'Stop PLC operation and motion before applying configuration'},
                    }});
                    assert.equal(params.expected_timeout_s, communicationSettings.timeout_s);
                    assert.equal(params.expected_status_retries, communicationSettings.status_retries);
                    communicationSettings = {...communicationSettings, timeout_s: params.timeout_s, status_retries: params.status_retries};
                }
                return json(communicationSettings);
            }
            if (command === 'test.start') {
                assert.equal(testToken, null);
                testToken = 'browser-session';
                state.operation = {mode: 'testing', test_active: true};
                state.selected_recipe_revision = 'browser-revision';
                return json({test_token: testToken, lease_ms: 5000, recipe_revision: 'browser-revision'});
            }
            if (command === 'test.renew') {
                assert.equal(params.test_token, testToken);
                return json({accepted: true, lease_ms: 5000});
            }
            if (command === 'test.stop') {
                if (params.test_token !== testToken) return json({accepted: false, stopped: false});
                testToken = null;
                state.operation = {mode: 'idle', test_active: false};
                state.motion = {state: 'stopped'};
                state.laser.on = false;
                return json({accepted: true, stopped: true, laser_on: false});
            }
            if (testToken && ['point.goto', 'laser.off', 'laser.pulse'].includes(command)) assert.equal(params.test_token, testToken);
            if (command === 'laser.off') { state.laser.on = false; return json({laser_on: false, permit_token: ++permitToken}); }
            if (command === 'point.goto' && params.test_token) {
                state.motion = {state: 'completed', completed_id: 'test-motion-' + ++testMotion};
                return json({motion_id: state.motion.completed_id, recipe_revision: 'browser-revision', laser_permit_token: permitToken});
            }
            if (command === 'laser.pulse' && params.test_token) { state.laser.on = true; return json({laser_on: true}); }
            if (command === 'recipe.list') return json({recipes: recipeList});
            if (command === 'recipe.pose_apply') {
                if (failRecipePose) return route.fulfill({json: {ok: false, error: {message: 'pose rejected'}}});
                assert.deepEqual(params, {recipe_id: 'pose-recipe', product_rotation_deg: 15,
                    product_tilt_deg: 3, product_translation_mm: [10, -20, 30]});
                recipePoseBackup = structuredClone(recipeList[0]);
                recipeList[0] = {...recipeList[0], product_rotation_deg: 15, product_tilt_deg: 3,
                    drawing: {...recipeList[0].drawing, product_rotation_deg: 15, product_tilt_deg: 3,
                        product_translation_mm: [10, -20, 30], pose_undo: {previous: recipePoseBackup},
                        points: recipeList[0].drawing.points.map(point => ({...point, posed_position: [100, 200, 300]}))},
                    points: recipeList[0].points.map(point => ({...point, pan: 12, tilt: 3}))};
                return json({recipe: recipeList[0]});
            }
            if (command === 'recipe.pose_undo') {
                recipeList[0] = recipePoseBackup;
                return json({recipe: recipeList[0]});
            }
            if (command === 'drawing.list') return json({drawings: drawingList});
            if (command === 'drawing.get') return json({drawing: {
                ...drawingList.find(drawing => drawing.id === params.drawing_id),
                points: [], point_sets: [], units: 'mm', coordinate_frame: 'PRODUCT_CAD_STEP_NATIVE',
            }});
            return json({accepted: true});
        }
        if (url.pathname === '/templates.js') return route.fulfill({contentType: 'text/javascript', body: ''});
        // Keep the real web console and exercise its CAD event/ready contract independently of WebGL.
        if (url.pathname === '/cad/app.js') return route.fulfill({contentType: 'text/javascript', body:
            "window.addEventListener('drawing-cad-load', e => {window.cadLoaded = e.detail.drawing.id; e.detail.ready = Promise.resolve();});"});
        const file = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[url.pathname];
        if (!file) return route.fulfill({status: 404, body: ''});
        return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
            body: fs.readFileSync(path.join(__dirname, '../pt503_tester/web', file))});
    });
    await page.addInitScript(() => {
        window.pads = [];
        window.sent = [];
        window.focused = true;
        Object.defineProperty(navigator, 'getGamepads', {value: () => window.pads});
        document.hasFocus = () => window.focused;
        const fetchOriginal = window.fetch;
        window.fetch = (url, options) => {
            if (options?.body) window.sent.push(JSON.parse(options.body));
            return fetchOriginal(url, options);
        };
    });
    const sent = command => page.evaluate(command => window.sent.filter(item => item.command === command), command);
    const clear = () => page.evaluate(() => {window.sent = [];});
    const tick = () => page.waitForTimeout(160);
    const ready = () => page.waitForFunction(() => padConnected && !padRequireCenter && !jogStopBusy);
    const press = (index, pressed) => page.evaluate(({index, pressed}) => {
        window.pads[0].buttons[index] = {pressed, value: Number(pressed)};
    }, {index, pressed});
    const axis = (index, value) => page.evaluate(({index, value}) => {window.pads[0].axes[index] = value;}, {index, value});
    const waitCommand = command => page.waitForFunction(command => window.sent.some(item => item.command === command), command);
    try {
        await page.goto('http://ptm.test/');
        await page.waitForFunction(() => window.cadLoaded === 'first');
        assert.equal(await page.locator('#drawingSelect').inputValue(), 'first', 'first drawing loads without a click');
        await page.waitForFunction(() => document.querySelector('#connection').classList.contains('connected'));
        assert.equal(await page.locator('#connection').evaluate(element => getComputedStyle(element, '::before').backgroundColor), 'rgb(20, 164, 111)');
        await page.locator('#tabs button').filter({hasText: /^설정$/}).click();
        await page.waitForFunction(() => !document.querySelector('#ptmStatusTimeout').disabled);
        assert.equal(await page.locator('#ptmStatusTimeout').inputValue(), '3');
        assert.equal(await page.locator('#ptmStatusRetries').inputValue(), '2');
        await page.locator('#ptmStatusTimeout').fill('1.5');
        await page.locator('#ptmStatusRetries').fill('4');
        await page.locator('#ptmCommunicationSettings button').click();
        await page.waitForFunction(() => !document.querySelector('#ptmStatusTimeout').disabled);
        assert.equal(communicationSettings.timeout_s, 1.5);
        assert.equal(communicationSettings.status_retries, 4);
        assert.equal(await page.locator('#ptmCommunicationState').innerText(), 'Core에 저장됨');
        failCommunicationSave = true;
        await page.locator('#ptmStatusRetries').fill('0');
        await page.locator('#ptmCommunicationSettings button').click();
        await page.waitForFunction(() => document.querySelector('#ptmCommunicationState').textContent.startsWith('저장 실패:'));
        assert.equal(communicationSettings.status_retries, 4, 'failed save preserves the Core value');
        failCommunicationSave = false;
        await page.locator('#refreshPtmCommunication').click();
        await page.waitForFunction(() => !document.querySelector('#ptmStatusRetries').disabled);
        assert.equal(await page.locator('#ptmStatusRetries').inputValue(), '4');
        await page.locator('#tabs button').filter({hasText: '프로토콜·고급 설정'}).click();
        const groups = await page.locator('#commandGroups strong').allTextContents();
        for (const group of ['camera', 'cruise', 'focus', 'lens', 'pattern', 'preset', 'scan', 'zoom', 'magnification', 'screen', 'zone', 'aux'])
            assert.equal(groups.includes(group), false, group + ' group is hidden');
        assert.deepEqual(await page.locator('#commandList strong').allTextContents(),
            commandNames.filter(command => ['system.ping', 'serial.scan', 'motion.absolute', 'point.goto',
                'laser.on', 'drawing.select_recipe', 'home.start'].includes(command)));
        state.motion.state = 'communication_timeout';
        await page.evaluate(() => refreshOnce());
        assert.equal(await page.locator('#connection').evaluate(element => element.classList.contains('connected')), false);
        state.motion.state = 'idle';
        state.serial.connected = false;
        await page.evaluate(() => refreshOnce());
        assert.equal(await page.locator('#connection').evaluate(element => element.classList.contains('connected')), false);
        state.serial.connected = true;
        failStatus = true;
        await page.evaluate(() => refreshOnce());
        assert.equal(await page.locator('#connection').innerText(), '서버 연결 끊김');
        failStatus = false;
        await page.evaluate(() => refreshOnce());

        await page.locator('#tabs button').filter({hasText: '도면 캘리브레이션'}).click();
        await page.waitForFunction(() => !document.querySelector('#drawingSelect').disabled);
        await page.locator('#drawingSelect').selectOption('second');
        await page.waitForFunction(() => window.cadLoaded === 'second');
        await page.locator('#tabs button').filter({hasText: '게임패드'}).click();
        assert.equal(await page.locator('#padInvertX, #padInvertY').count(), 0);
        await page.evaluate(() => {window.pads = [{index: 0, id: 'Test standard', mapping: 'standard',
            connected: true, axes: [0, 0, 0.8, 0],
            buttons: Array.from({length: 19}, () => ({pressed: false, value: 0}))}];});
        await page.waitForFunction(() => document.querySelector('#padEnable').checked);
        assert.equal(await page.locator('#padMappings tr').count(), 19, 'all reported buttons, including extra keys, are configurable');
        await clear();
        await tick();
        assert.equal((await sent('motion.jog')).length, 0, 'connection with a tilted stick never starts motion');
        await axis(2, 0);
        await ready();
        await axis(2, 0.8);
        await waitCommand('motion.jog');
        assert.equal((await sent('motion.jog')).at(-1).params.pan, 'right');
        await clear();
        await axis(2, 0);
        await waitCommand('motion.stop');
        assert.equal((await sent('motion.stop')).at(-1).params.preserve_laser_manual, true);

        await page.locator('#padX').selectOption('0');
        await page.waitForFunction(() => document.querySelector('#padStatus').textContent.includes('매핑 저장됨'));
        await ready();
        await clear();
        await axis(0, -0.8);
        await waitCommand('motion.jog');
        assert.equal((await sent('motion.jog')).at(-1).params.pan, 'left', 'configured axis drives actual motion');
        await axis(0, 0);
        await tick();
        await tick();

        await page.locator('#padMappings tr[data-input="b18"] select').selectOption('laser');
        await ready();
        await clear();
        await press(18, true);
        await waitCommand('laser.on');
        await tick();
        assert.equal((await sent('laser.on')).length, 1, 'holding a mapped button fires once');
        await press(18, false);
        await tick();
        await page.locator('#padMappings tr[data-input="b17"] select').selectOption('pan_left');
        await ready();
        await clear();
        await press(17, true);
        await waitCommand('motion.jog');
        assert.equal((await sent('motion.jog')).at(-1).params.pan, 'left');
        await clear();
        await press(1, true);
        await press(18, true);
        await waitCommand('motion.stop');
        assert.equal((await sent('laser.on')).length, 0, 'STOP takes priority over laser and jog');
        assert.equal((await sent('motion.stop')).at(-1).params.preserve_laser_manual, false);
        await press(1, false);
        await tick();
        assert.equal((await sent('motion.jog')).length, 0, 'held direction cannot restart after STOP');
        await press(17, false);
        await press(18, false);
        await ready();
        await press(17, true);
        await waitCommand('motion.jog');
        await clear();
        await page.evaluate(() => {window.focused = false;});
        await waitCommand('motion.stop');
        await page.evaluate(() => {window.focused = true;});
        await press(17, false);
        await tick();

        await page.reload();
        await page.locator('#tabs button').filter({hasText: '게임패드'}).click();
        await page.evaluate(() => {window.pads = [{index: 0, id: 'Test standard', mapping: 'standard', connected: true,
            axes: [0, 0, 0, 0], buttons: Array.from({length: 19}, () => ({pressed: false, value: 0}))}];});
        await page.waitForFunction(() => document.querySelector('#padEnable').checked);
        assert.equal(await page.locator('#padX').inputValue(), '0');
        assert.equal(await page.locator('#padMappings tr[data-input="b18"] select').inputValue(), 'laser');
        await page.locator('#padDisconnect').click();
        await tick();
        assert.equal(await page.locator('#padEnable').isChecked(), false, 'manual disconnect stays disconnected');
        await page.locator('#padConnect').click();
        await ready();
        await clear();
        await axis(0, 0.8);
        await waitCommand('motion.jog');
        await clear();
        await page.evaluate(() => {window.pads = [];});
        await waitCommand('motion.stop');
        await page.evaluate(() => {window.pads = [{index: 0, id: 'Test raw', mapping: '', connected: true,
            axes: [0, 0, 0, 0, 0, 0, 0, 0, 0, 3.285714],
            buttons: Array.from({length: 12}, () => ({pressed: false, value: 0}))}];});
        await page.waitForFunction(() => document.querySelectorAll('#padMappings tr').length === 16);
        assert.equal(await page.locator('#padPov').inputValue(), '9');
        await page.locator('#padMappings tr[data-input="p0"] select').selectOption('laser_off');
        await ready();
        await clear();
        await axis(9, -1);
        await waitCommand('laser.off');
        await axis(9, 3.285714);
        await tick();
        for (const width of [1440, 390]) {
            await page.setViewportSize({width, height: 1000});
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        }
        await clear();
        await page.evaluate(() => {
            recipeTestRunning = true;
            recipeTestAbort = false;
            updateRecipeTestButtons();
        });
        assert.equal(await page.locator('#runRecipeSequence').isDisabled(), true);
        assert.equal(await page.locator('#recipeSelect').isDisabled(), true);
        assert.match(await page.evaluate(() => cmd('point.goto', {}).catch(error => error.message)), /테스트를 먼저 정지/);
        assert.equal((await sent('point.goto')).length, 0, 'competing point commands never leave the browser');
        assert.match(await page.evaluate(() => cmd('drawing.export_recipe', {}).catch(error => error.message)), /테스트를 먼저 정지/);
        assert.equal((await sent('drawing.export_recipe')).length, 0, 'drawing changes cannot mutate the test recipe');
        assert.ok(Array.isArray((await page.evaluate(() => cmd('drawing.list', {}, true))).drawings), 'readonly drawing lists remain available');
        await page.evaluate(() => stopJog());
        assert.equal(await page.evaluate(() => recipeTestAbort), true, 'ordinary STOP also aborts an active recipe test');
        assert.equal((await sent('motion.stop')).length, 1);
        assert.equal((await sent('laser.off')).length, 1);
        await page.evaluate(() => { recipeTestRunning = false; recipeTestAbort = false; updateRecipeTestButtons(); });
        state.operation = {mode: 'testing', test_active: true};
        await page.evaluate(() => refreshOnce());
        assert.equal(await page.locator('#runRecipeSequence').isDisabled(), true, 'observer pages reflect an active remote test');
        assert.equal(await page.locator('#stopRecipeTest').isDisabled(), false, 'an operator can still explicitly stop the remote test');
        assert.match(await page.evaluate(() => cmd('motion.jog', {}).catch(error => error.message)), /테스트를 먼저 정지/);
        state.operation = {mode: 'idle', test_active: false};
        await page.evaluate(() => refreshOnce());
        recipeList = [{id: 'pose-recipe', name: 'Pose test', index: 7,
            drawing: {product_rotation_deg: 0, product_tilt_deg: 0, product_translation_mm: [0, 0, 0],
                calibration: {rotation_axis: 'X', tilt_axis: 'Y'},
                points: [1, 2].map(id => ({id: 'source-' + id, x: id, y: 2, z: 3}))},
            points: [1, 2].map(order => ({id: String(order), order, enabled: true, name: 'P' + order,
                pan: order, tilt: 0, dwell_ms: 0, drawing_point_id: 'source-' + order}))}];
        await page.evaluate(async () => {jog = null; padActive = false; await loadRecipes('pose-recipe');});
        await page.locator('#tabs button').filter({hasText: /^레시피·포인트$/}).click();
        for (const [id, value] of [['recipePoseTurn', '15'], ['recipePoseTilt', '3'],
            ['recipePoseX', '10'], ['recipePoseY', '-20'], ['recipePoseZ', '30']]) await page.locator('#' + id).fill(value);
        await page.evaluate(() => refreshOnce());
        assert.equal(await page.locator('#recipePoseX').inputValue(), '10', 'polling preserves unsaved pose input');
        await clear();
        await page.locator('#applyRecipePose').click();
        await page.waitForFunction(() => !document.querySelector('#undoRecipePose').disabled);
        assert.equal((await sent('recipe.pose_apply')).length, 1);
        assert.equal((await sent('point.goto')).length, 0, 'applying pose only changes saved targets');
        assert.equal(await page.locator('#recipePoseTurn').inputValue(), '15');
        for (const row of await page.locator('#points tr').allTextContents()) assert.match(row, /100\.000/);
        await page.locator('#undoRecipePose').click();
        await page.waitForFunction(() => !recipePoseBusy && document.querySelector('#undoRecipePose').disabled);
        assert.equal(await page.locator('#recipePoseTurn').inputValue(), '0');
        assert.equal(await page.locator('#recipePoseX').inputValue(), '0');
        failRecipePose = true;
        await page.locator('#recipePoseTurn').fill('15');
        await page.locator('#applyRecipePose').click();
        await page.waitForFunction(() => document.querySelector('#recipePoseStatus').textContent.includes('pose rejected'));
        assert.equal(await page.locator('#recipePoseTurn').inputValue(), '15', 'failed apply preserves operator input');
        assert.equal(recipeList[0].points[0].pan, 1, 'failed apply preserves saved targets');
        failRecipePose = false;
        recipeList = [{id: 'session-recipe', name: 'Session test', index: 1, points: [1, 2].map(order => ({
            id: String(order), order, enabled: true, name: 'Point ' + order, pan: order, tilt: 0, dwell_ms: 0,
        }))}];
        await page.evaluate(async () => {
            jog = null; padActive = false;
            await loadRecipes('session-recipe');
            $('recipeTestLaserMs').value = '600';
        });
        await clear();
        await page.evaluate(() => runRecipePointTest('sequence'));
        assert.equal((await sent('test.start')).length, 1);
        assert.ok((await sent('test.renew')).length >= 1, 'real timers renew the session during illumination');
        assert.equal((await sent('test.stop')).length, 1);
        assert.deepEqual((await sent('point.goto')).map(request => request.params.point_id), ['1', '2']);
        assert.ok((await sent('point.goto')).every(request => request.params.test_token === 'browser-session'));
        assert.equal((await sent('motion.stop')).length, 0, 'normal point gaps never issue a global motor STOP');
        assert.equal(await page.locator('#recipeTestStatus').innerText(), '순차 테스트 완료');
        assert.equal(await page.evaluate(() => recipeTestSession), null);
        assert.equal(await page.locator('#runRecipeSequence').isDisabled(), false, 'successful cleanup immediately releases the local controls');
        assert.equal(testToken, null);
        drawingList = [];
        await page.reload();
        await page.waitForFunction(() => window.sent.some(item => item.command === 'drawing.list'));
        assert.equal((await sent('drawing.get')).length, 0, 'empty drawing list does not request a drawing');
        assert.deepEqual(errors, []);
        console.log('PASS: browser two-point test session/renewal/cleanup, remote ownership UI, protocol, drawings, gamepad STOP/focus/reconnect and mobile layout');
    } catch (error) {
        console.error({tabs: await page.locator('#tabs').innerText(), notice: await page.locator('#notice').innerText(), errors,
            pad: await page.evaluate(() => ({padConnected, padIndex, padMapping, padRequireCenter, jogStopBusy,
                padDisconnecting, focused: document.hasFocus(), hidden: document.hidden, status, sent: window.sent.slice(-5)}))});
        throw error;
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});

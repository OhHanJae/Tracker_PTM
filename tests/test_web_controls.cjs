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
            if (command === 'laser.off') return json({laser_on: false});
            if (command === 'recipe.list') return json({recipes: []});
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
        drawingList = [];
        await page.reload();
        await page.waitForFunction(() => window.sent.some(item => item.command === 'drawing.list'));
        assert.equal((await sent('drawing.get')).length, 0, 'empty drawing list does not request a drawing');
        assert.deepEqual(errors, []);
        console.log('PASS: protocol filtering, connection colors, initial/changed drawings, full mapping, persistence, standard/RAW inputs, STOP, focus, unplug/reconnect, mobile layout');
    } catch (error) {
        console.error({tabs: await page.locator('#tabs').innerText(), notice: await page.locator('#notice').innerText(), errors,
            pad: await page.evaluate(() => ({padConnected, padIndex, padMapping, padRequireCenter, jogStopBusy,
                padDisconnecting, focused: document.hasFocus(), hidden: document.hidden, status, sent: window.sent.slice(-5)}))});
        throw error;
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});

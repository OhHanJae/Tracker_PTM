const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../pt503_tester/web/app.js'), 'utf8');
const code = source.slice(source.indexOf('async function waitForRecipeMotion('), source.indexOf('function clearPoint('));

function runtime() {
    let now = 0;
    let timerId = 0;
    const timers = new Map();
    const duration = {value: '1000'};
    const ctx = {
        Date: {now: () => now}, Math,
        AbortController,
        setTimeout(callback, ms) {const id = ++timerId; timers.set(id, {callback, at: now + ms}); return id;},
        clearTimeout(id) {timers.delete(id);},
        async advance(ms) {
            now += ms;
            for (const [id, timer] of [...timers]) {
                if (timer.at > now || !timers.delete(id)) continue;
                timer.callback();
            }
            for (let count = 0; count < 8; count++) await Promise.resolve();
        },
        status: {serial: {connected: true}, motion: {completion: {timeout_s: 1}}, selected_recipe_revision: 7},
        recipeTestRunning: false, recipeTestAbort: false,
        recipeTestCommandPromise: null, recipeTestStopPromise: null,
        recipeTestSession: null, activeToken: null, sessionEvents: [],
        localManualOutput: false,
        jog: null, jogBusy: false, jogStopBusy: false, padActive: false,
        selectedPointId: null, calls: [], sleeps: [], labels: [], events: [], laserOn: false, permitToken: 0,
        $: id => id === 'recipeTestLaserMs' ? duration : null,
        renderPoints() {}, updateRecipeTestButtons() {},
        setRecipeTestStatus(label) {ctx.labels.push(label);},
        pointLabel: point => point.id,
        async sleep(ms) {ctx.sleeps.push(ms); ctx.events.push(['wait', ms]); await ctx.advance(ms);},
        async refreshOnce() {
            ctx.events.push(['completed', ctx.motionId]);
            ctx.status.motion = {state: 'completed', completed_id: ctx.motionId};
        },
        async cmd(command, params) {
            if (command === 'system.status') return ctx.status;
            if (command === 'test.start') {
                if (ctx.status.serial?.connected !== true || !ctx.status.motion || ctx.status.motion.id
                    || ['jog', 'tracking', 'homing'].includes(ctx.status.motion.state)) throw Error('장치 연결과 진행 중인 이동을 확인하세요.');
                if (ctx.status.laser?.manual_allowed === false) throw Error(ctx.status.laser.manual_block_reason);
                assert.equal(ctx.activeToken, null);
                ctx.activeToken = 'test-' + (ctx.sessionEvents.length + 1);
                ctx.sessionEvents.push(['start', ctx.activeToken]);
                return {test_token: ctx.activeToken, lease_ms: 5000, recipe_revision: 7};
            }
            if (command === 'test.renew') {
                assert.equal(params.test_token, ctx.activeToken);
                ctx.sessionEvents.push(['renew', params.test_token]);
                return {accepted: true, lease_ms: 5000};
            }
            if (command === 'test.stop') {
                ctx.sessionEvents.push(['stop', params.test_token]);
                if (params.test_token !== ctx.activeToken) return {accepted: false, stopped: false};
                ctx.activeToken = null;
                ctx.events.push(['stop'], ['off']);
                ctx.laserOn = false;
                ctx.status.motion = {state: 'stopped'};
                return {accepted: true, stopped: true, laser_on: false};
            }
            if (params.test_token && params.test_token !== ctx.activeToken) throw Error('TEST_SESSION_EXPIRED');
            if (ctx.activeToken && ['point.goto', 'laser.off', 'laser.pulse'].includes(command)) {
                assert.equal(params.test_token, ctx.activeToken, 'every test mutation carries the owning session token');
            }
            if (command === 'motion.stop') {
                ctx.activeToken = null;
                ctx.events.push(['stop']);
                ctx.status.motion = {state: 'stopped'};
                return {stopped: true};
            }
            if (command === 'laser.off') {
                ctx.events.push(['off']);
                ctx.laserOn = false;
                return {laser_on: false, permit_token: ++ctx.permitToken};
            }
            if (command === 'laser.pulse') {
                assert.equal(ctx.status.motion.completed_id, ctx.motionId, 'laser waits for the matching motion');
                assert.equal(params.expected_motion_id, ctx.motionId);
                assert.equal(params.expected_recipe_revision, 7);
                assert.equal(params.laser_permit_token, ctx.permitToken, 'pulse is bound to the unchanged move lease');
                assert.equal(params.recipe_id, 'recipe');
                assert.equal(params.point_id, ctx.calls.at(-1));
                ctx.events.push(['on', params.duration_ms]);
                ctx.laserOn = true;
                return {laser_on: true};
            }
            assert.equal(command, 'point.goto');
            assert.equal(ctx.laserOn, false, 'laser is OFF during every move');
            assert.equal(params.laser_after_move, false, 'test owns laser timing');
            assert.equal(params.exclusive, true, 'test motion protects its target from competing commands');
            assert.equal(params.laser_permit_token, ctx.permitToken, 'late moves are bound to the preceding confirmed OFF');
            if (ctx.calls.length) assert.equal(params.expected_recipe_revision, 7, 'later points stay pinned to the first accepted recipe revision');
            ctx.events.push(['move', params.point_id]);
            ctx.calls.push(params.point_id);
            ctx.motionId = 'motion-' + ctx.calls.length;
            return {motion_id: ctx.motionId, recipe_revision: 7, laser_permit_token: ctx.permitToken};
        },
        selected: () => ({id: 'recipe'}),
        enabledRecipePoints: () => [{id: '1', dwell_ms: 0}, {id: '2', dwell_ms: 0}],
    };
    vm.createContext(ctx);
    vm.runInContext(code, ctx);
    return ctx;
}

function installRealCommand(ctx) {
    const command = ctx.cmd;
    const notice = {dataset: {}};
    const duration = ctx.$('recipeTestLaserMs');
    ctx.$ = id => id === 'notice' ? notice : id === 'recipeTestLaserMs' ? duration : null;
    ctx.request = async (_path, body, signal) => {
        const pending = command(body.command, body.params);
        if (!signal) return pending;
        return Promise.race([pending, new Promise((_resolve, reject) => {
            if (signal.aborted) return reject(Error('aborted'));
            signal.addEventListener('abort', () => reject(Error('aborted')), {once: true});
        })]);
    };
    ctx.log = () => {};
    ctx.drawingActionBusy = false;
    ctx.activeDrawing = null;
    vm.runInContext(source.slice(source.indexOf('async function cmd('), source.indexOf('function guarded(')), ctx);
}

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
}

(async () => {
    let ctx = runtime();
    ctx.$('recipeTestLaserMs').value = '125';
    await ctx.runRecipePointTest('sequence');
    assert.deepEqual(ctx.calls, ['1', '2']);
    assert.deepEqual(ctx.events, [
        ['off'], ['move', '1'], ['completed', 'motion-1'], ['on', 125],
        ['wait', 50], ['wait', 50], ['wait', 25], ['off'],
        ['off'], ['move', '2'], ['completed', 'motion-2'], ['on', 125],
        ['wait', 50], ['wait', 50], ['wait', 25], ['off'],
        ['stop'], ['off'],
    ], 'each point completes, illuminates for the configured duration, then turns OFF before the next move');
    assert.equal(ctx.laserOn, false, 'sequence ends with laser OFF');
    assert.equal(ctx.labels.at(-1), '순차 테스트 완료');
    assert.equal(ctx.recipeTestSession, null);
    assert.deepEqual(ctx.sessionEvents.map(event => event[0]), ['start', 'stop'], 'one session spans both points and the gap');

    ctx = runtime();
    ctx.refreshOnce = async () => {
        ctx.status.motion = {state: 'completed', completed_id: ctx.motionId};
        if (ctx.calls.length === 3) ctx.recipeTestAbort = true;
    };
    await ctx.runRecipePointTest('random');
    assert.equal(ctx.calls.length, 3);
    assert.notEqual(ctx.calls[0], ctx.calls[1]);
    assert.notEqual(ctx.calls[1], ctx.calls[2]);
    assert.equal(ctx.events.filter(event => event[0] === 'on').length, 2, 'stop during movement does not illuminate');
    assert.equal(ctx.laserOn, false);

    ctx = runtime();
    let polls = 0;
    ctx.refreshOnce = async () => {
        ctx.status.motion = ++polls === 1
            ? {state: 'completed', completed_id: 'old'}
            : {state: 'completed', completed_id: ctx.motionId};
    };
    await ctx.moveRecipeTestPoint({id: 'recipe'}, {id: '1', dwell_ms: 0}, 'test', 50);
    assert.equal(polls, 2, 'stale completion does not start next move');
    assert.deepEqual(ctx.sleeps, [50, 50]);

    for (const state of ['timeout', 'fault', 'stopped']) {
        ctx = runtime();
        ctx.refreshOnce = async () => {ctx.status.motion = {state, failed_id: ctx.motionId, last_error: 'failed'};};
        await assert.rejects(ctx.runRecipePointTest('sequence'), /failed/);
        assert.deepEqual(ctx.calls, ['1']);
        assert.equal(ctx.events.some(event => event[0] === 'on'), false);
    }
    ctx = runtime();
    ctx.refreshOnce = async () => {ctx.status = {};};
    await assert.rejects(ctx.runRecipePointTest('sequence'), /상태/);
    assert.deepEqual(ctx.calls, ['1']);

    ctx = runtime();
    ctx.refreshOnce = async () => {ctx.status.motion = {state: 'tracking', id: ctx.motionId};};
    await assert.rejects(ctx.runRecipePointTest('sequence'), /초과/);
    assert.deepEqual(ctx.calls, ['1']);

    ctx = runtime();
    ctx.$('recipeTestLaserMs').value = '125';
    ctx.enabledRecipePoints = () => [{id: '1', dwell_ms: 5000}];
    await ctx.runRecipePointTest('sequence');
    assert.deepEqual(ctx.sleeps, [50, 50, 25], 'test setting controls laser duration without changing stored point dwell');

    ctx = runtime();
    ctx.sleep = async () => {ctx.recipeTestAbort = true;};
    await ctx.runRecipePointTest('sequence');
    assert.equal(ctx.labels.at(-1), '테스트 정지', 'stop interrupts dwell');
    assert.equal(ctx.recipeTestRunning, false);
    assert.equal(ctx.laserOn, false, 'stop during illumination turns laser OFF');
    assert.deepEqual(ctx.calls, ['1'], 'stop prevents another move');

    for (const duration of ['', '0', '49', '60001', 'NaN', '50.5']) {
        ctx = runtime();
        ctx.$('recipeTestLaserMs').value = duration;
        await assert.rejects(ctx.runRecipePointTest('sequence'), /조사 시간/);
        assert.deepEqual(ctx.calls, [], 'invalid duration does not start movement');
    }

    ctx = runtime();
    ctx.status.laser = {manual_allowed: false, manual_block_reason: 'PLC 강제정지(FORCE_STOP) 활성'};
    await assert.rejects(ctx.runRecipePointTest('sequence'), /FORCE_STOP/);
    assert.deepEqual(ctx.calls, [], 'Core interlock is checked before the first move');

    ctx = runtime();
    const command = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'laser.pulse') throw Error('laser rejected');
        return command(name, params);
    };
    await assert.rejects(ctx.runRecipePointTest('sequence'), /laser rejected/);
    assert.deepEqual(ctx.calls, ['1'], 'laser failure prevents another move');
    assert.equal(ctx.events.at(-1)[0], 'off', 'failed ON still attempts OFF');

    ctx = runtime();
    const offCommand = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'laser.off' && ctx.laserOn) throw Error('OFF not confirmed');
        return offCommand(name, params);
    };
    await assert.rejects(ctx.runRecipePointTest('sequence'), /OFF not confirmed/);
    assert.deepEqual(ctx.calls, ['1'], 'unconfirmed OFF blocks the next move');

    ctx = runtime();
    ctx.refreshOnce = async () => {
        ctx.status.motion = {state: 'tracking', completed_id: ctx.motionId, id: 'foreign-motion'};
    };
    await assert.rejects(ctx.runRecipePointTest('sequence'), /다른 이동/);
    assert.equal(ctx.events.some(event => event[0] === 'on'), false, 'old completion cannot mask a replacing PLC move');

    ctx = runtime();
    const preview = deferred();
    let previews = 0;
    ctx.selected = () => ({id: 'recipe', product_rotation_deg: 5});
    const previewCommand = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'recipe.pose_preview') { previews++; await preview.promise; return {}; }
        return previewCommand(name, params);
    };
    const sequence = ctx.runRecipePointTest('sequence');
    assert.equal(ctx.recipeTestRunning, true, 'preparation reserves the run before its first await');
    for (let count = 0; count < 8; count++) await Promise.resolve();
    await ctx.runRecipePointTest('random');
    assert.equal(previews, 1, 'double click cannot start a second test during pose preview');
    preview.resolve();
    await sequence;
    assert.deepEqual(ctx.calls, ['1', '2']);

    for (const commandName of ['motion.jog', 'point.goto', 'point.select', 'point.reorder', 'recipe.upsert', 'laser.on',
        'drawing.point_upsert', 'drawing.point_delete', 'drawing.point_reorder', 'drawing.calibrate',
        'drawing.reset_calibration', 'drawing.export_recipe', 'drawing.import_recipe',
        'drawing.select_recipe', 'drawing.select_point_set', 'drawing.delete', 'drawing.update', 'drawing.reorder']) {
        ctx = runtime();
        installRealCommand(ctx);
        ctx.recipeTestRunning = true;
        await assert.rejects(ctx.cmd(commandName, {}), /테스트를 먼저 정지/);
        assert.deepEqual(ctx.events, [], commandName + ' cannot race with a test');
    }

    for (const commandName of ['recipe.export', 'recipe.pose_preview', 'drawing.list', 'drawing.get', 'drawing.export',
        'drawing.estimate_recipe', 'drawing.estimate_pan_tilt', 'drawing.estimate_xy']) {
        ctx = runtime();
        ctx.cmd = async name => { assert.equal(name, commandName); return {read: true}; };
        installRealCommand(ctx);
        ctx.recipeTestRunning = true;
        assert.equal((await ctx.cmd(commandName)).read, true, commandName + ' remains available during a test');
    }

    ctx = runtime();
    installRealCommand(ctx);
    const dwellSleep = ctx.sleep;
    ctx.sleep = async ms => { await dwellSleep(ms); await ctx.cmd('motion.stop'); };
    await ctx.runRecipePointTest('sequence');
    assert.deepEqual(ctx.calls, ['1'], 'ordinary STOP during dwell cancels the sequence');
    assert.equal(ctx.laserOn, false);
    assert.equal(ctx.recipeTestRunning, false);

    for (const blockedCommand of ['point.goto', 'laser.pulse']) {
        ctx = runtime();
        const entered = deferred();
        const release = deferred();
        const actualCommand = ctx.cmd;
        ctx.cmd = async (name, params) => {
            if (name === blockedCommand) {
                entered.resolve();
                await release.promise;
            }
            return actualCommand(name, params);
        };
        installRealCommand(ctx);
        const running = ctx.runRecipePointTest('sequence');
        await entered.promise;
        const stopping = ctx.cmd('motion.stop');
        for (let count = 0; count < 10; count++) await Promise.resolve();
        assert.ok(ctx.events.some(event => event[0] === 'stop'), 'STOP is sent before waiting for an old HTTP request');
        assert.equal(ctx.recipeTestRunning, true, 'new tests stay locked while stop is pending');
        release.resolve();
        await Promise.all([running, stopping]);
        assert.equal(ctx.events.some(event => event[0] === 'on'), false, 'a cancelled token can never illuminate');
        assert.equal(ctx.laserOn, false);
        assert.equal(ctx.events.at(-1)[0], 'off');
        assert.equal(ctx.calls.length, blockedCommand === 'point.goto' ? 0 : 1, 'late requests with the ended token are rejected');
    }

    for (const change of ['motion', 'policy', 'revision']) {
        ctx = runtime();
        const originalSleep = ctx.sleep;
        ctx.sleep = async ms => {
            await originalSleep(ms);
            if (change === 'motion') ctx.status.motion = {state: 'tracking', id: 'plc-move', completed_id: ctx.motionId};
            else if (change === 'policy') ctx.status.laser = {manual_allowed: false, manual_block_reason: 'PLC mode changed'};
            else ctx.status.selected_recipe_revision = 8;
        };
        await assert.rejects(ctx.runRecipePointTest('sequence'), /중단|PLC mode changed/);
        assert.deepEqual(ctx.calls, ['1'], change + ' change during dwell blocks the next point');
        assert.equal(ctx.laserOn, false);
        assert.ok(ctx.events.some(event => event[0] === 'stop'), 'external intervention also sends motor STOP');
        if (change === 'motion') assert.equal(ctx.status.motion.state, 'stopped', 'the replacing PLC move is stopped under the intervention policy');
    }

    ctx = runtime();
    ctx.status.motion = {state: 'tracking', id: 'plc-move'};
    await assert.rejects(ctx.runRecipePointTest('sequence'), /진행 중인 이동/);
    assert.deepEqual(ctx.events, [], 'test start does not stop an already active PLC move');

    ctx = runtime();
    ctx.jogBusy = true;
    await assert.rejects(ctx.runRecipePointTest('sequence'), /수동 운전을 정지/);
    assert.equal(ctx.recipeTestRunning, false, 'an in-flight manual JOG cannot overlap test preparation');

    for (const unavailable of [{}, {serial: {connected: false}, motion: {state: 'idle'}}]) {
        ctx = runtime();
        ctx.status = unavailable;
        await assert.rejects(ctx.runRecipePointTest('sequence'), /장치 연결/);
        assert.deepEqual(ctx.calls, [], 'missing or disconnected status never starts a move');
    }

    ctx = runtime();
    const readCommand = ctx.cmd;
    ctx.cmd = async (name, params) => name === 'motion.completion_config' ? {timeout_s: 30} : readCommand(name, params);
    installRealCommand(ctx);
    ctx.recipeTestRunning = true;
    assert.equal((await ctx.cmd('motion.completion_config')).timeout_s, 30, 'readonly completion settings stay available');
    await assert.rejects(ctx.cmd('motion.completion_config', {timeout_s: 10}), /테스트를 먼저 정지/);

    ctx = runtime();
    const stopCommand = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'motion.stop') throw Error('STOP failed');
        return stopCommand(name, params);
    };
    await assert.rejects(ctx.stopRecipePointTest(), /STOP failed/);
    assert.equal(ctx.events.at(-1)[0], 'off', 'a failed motor STOP still attempts laser OFF');

    ctx = runtime();
    vm.runInContext(source.slice(source.indexOf('function enabledRecipePoints('), source.indexOf('function pointLabel(')), ctx);
    assert.deepEqual(Array.from(ctx.enabledRecipePoints({points: [
        {id: '3', order: 3, enabled: true}, {id: '1', order: 1, enabled: true},
        {id: '2', order: 2, enabled: false},
    ]}), point => point.id), ['1', '3'], 'sequence starts at the first enabled order');

    const beacons = [];
    let leave;
    const leaveContext = {
        recipeTestAbort: false, recipeTestSession: null, localManualOutput: false, status: {}, Blob,
        window: {addEventListener: (_event, callback) => { leave = callback; }},
        navigator: {sendBeacon: (_path, blob) => beacons.push(blob)},
    };
    const leaveStart = source.search(/window\.addEventListener\(\r?\n    'pagehide',/);
    assert.ok(leaveStart >= 0);
    vm.runInNewContext(source.slice(leaveStart, source.indexOf('let refreshTimer = null;', leaveStart)), leaveContext);
    leave();
    assert.equal(leaveContext.recipeTestAbort, true);
    assert.equal(beacons.length, 0, 'closing an idle observer page never sends global STOP');
    leaveContext.localManualOutput = true;
    leave();
    assert.deepEqual(await Promise.all(beacons.map(async blob => JSON.parse(await blob.text()).command)),
        ['motion.stop', 'laser.off'], 'leaving invalidates both delayed moves and laser pulses');
    assert.ok((await Promise.all(beacons.map(async blob => JSON.parse(await blob.text()).params.if_no_test_session))).every(Boolean),
        'stale manual pages only stop outputs when no newer test is active');
    beacons.length = 0;
    leaveContext.recipeTestSession = {token: 'old-test'};
    leave();
    assert.deepEqual(JSON.parse(await beacons[0].text()), {command: 'test.stop', params: {test_token: 'old-test'}});
    assert.equal(beacons.length, 1, 'leaving an owned test never sends a global STOP that could affect a newer session');
    beacons.length = 0;
    leaveContext.recipeTestSession = null;
    leaveContext.status = {operation: {test_active: true}};
    leave();
    assert.equal(beacons.length, 0, 'leaving an observer page does not stop another test');

    ctx = runtime();
    await ctx.runRecipePointTest('sequence');
    assert.ok(ctx.sessionEvents.some(event => event[0] === 'renew'), 'lease stays alive through movement and illumination');
    assert.equal(new Set(ctx.sessionEvents.map(event => event[1])).size, 1, 'renew and cleanup use the same token');

    for (const failure of ['reject', 'hang']) {
        ctx = runtime();
        ctx.enabledRecipePoints = () => [{id: '1'}];
        ctx.$('recipeTestLaserMs').value = failure === 'hang' ? '6000' : '2000';
        const liveCommand = ctx.cmd;
        ctx.cmd = async (name, params) => {
            if (name === 'test.renew') {
                if (failure === 'hang') return new Promise(() => {});
                throw Error('renew offline');
            }
            return liveCommand(name, params);
        };
        installRealCommand(ctx);
        await assert.rejects(ctx.runRecipePointTest('sequence'), /연결 유지/);
        assert.equal(ctx.recipeTestRunning, false, failure + ' renew releases the local execution lock');
        assert.equal(ctx.recipeTestSession, null);
        assert.equal(ctx.laserOn, false);
        ctx.$('recipeTestLaserMs').value = '50';
        await ctx.runRecipePointTest('sequence');
        assert.equal(ctx.recipeTestRunning, false, 'another run is possible after timeout cleanup');
    }

    ctx = runtime();
    const started = deferred();
    const startReply = deferred();
    const startCommand = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'test.start') { started.resolve(); await startReply.promise; }
        return startCommand(name, params);
    };
    const preparing = ctx.runRecipePointTest('sequence');
    await started.promise;
    const cancelPreparation = ctx.stopRecipePointTest();
    startReply.resolve();
    await Promise.all([preparing, cancelPreparation]);
    assert.deepEqual(ctx.calls, [], 'STOP during reservation prevents the first move');
    assert.equal(ctx.activeToken, null, 'a late start token is still cancelled');
    assert.equal(ctx.recipeTestRunning, false);

    ctx = runtime();
    ctx.activeToken = 'new-session';
    ctx.recipeTestSession = {token: 'old-session', controller: new AbortController()};
    installRealCommand(ctx);
    const stale = await ctx.stopRecipePointTest();
    assert.equal(stale.accepted, false);
    assert.equal(ctx.activeToken, 'new-session');
    assert.deepEqual(ctx.events, [], 'stale cleanup never falls back to untokened STOP or OFF');

    ctx = runtime();
    ctx.activeToken = 'new-session';
    ctx.recipeTestSession = {token: 'old-session', controller: new AbortController()};
    installRealCommand(ctx);
    const operatorStop = await ctx.stopRecipePointTest({operator: true});
    assert.equal(operatorStop.laser_on, false);
    assert.equal(ctx.activeToken, null, 'an explicit operator STOP also cancels a newer active session');
    assert.deepEqual(ctx.events, [['stop'], ['off']]);

    ctx = runtime();
    const failedOperatorStop = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'motion.stop') throw Error('operator STOP failed');
        return failedOperatorStop(name, params);
    };
    installRealCommand(ctx);
    await assert.rejects(ctx.stopOperatorOutputs(), /operator STOP failed/);
    assert.deepEqual(ctx.events, [['off']], 'an explicit STOP transport failure still attempts laser OFF separately');

    ctx = runtime();
    ctx.status.operation = {mode: 'testing', test_active: true};
    await assert.rejects(ctx.runRecipePointTest('sequence'), /다른 포인트 테스트/);
    assert.equal(ctx.recipeTestRunning, false, 'an observer cannot start a competing local run');

    ctx = runtime();
    const startNeverReplies = deferred();
    ctx.cmd = async (name) => {
        if (name === 'test.start') { startNeverReplies.resolve(); return new Promise(() => {}); }
        throw Error('unexpected command');
    };
    installRealCommand(ctx);
    const noStartReply = ctx.runRecipePointTest('sequence');
    await startNeverReplies.promise;
    await ctx.advance(5001);
    await assert.rejects(noStartReply, /시작 응답/);
    assert.equal(ctx.recipeTestRunning, false);
    assert.equal(ctx.recipeTestSession, null, 'a lost reservation reply cannot hold the local lock forever');

    ctx = runtime();
    ctx.$('recipeTestLaserMs').value = '50';
    const stopNeverReplies = deferred();
    const normalCommand = ctx.cmd;
    ctx.cmd = async (name, params) => {
        if (name === 'test.stop') { stopNeverReplies.resolve(); return new Promise(() => {}); }
        return normalCommand(name, params);
    };
    installRealCommand(ctx);
    const noStopReply = ctx.runRecipePointTest('sequence');
    await stopNeverReplies.promise;
    await ctx.advance(2001);
    await assert.rejects(noStopReply, /aborted/);
    assert.equal(ctx.recipeTestRunning, false);
    assert.equal(ctx.recipeTestSession, null, 'lost stop acknowledgements also release the local run lock');

    console.log('PASS: sequence/random session ownership, renewal timeout cleanup, stale-token isolation, reservation STOP, ordering and collision regressions');
})().catch(error => {console.error(error); process.exitCode = 1;});

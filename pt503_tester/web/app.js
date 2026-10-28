'use strict';

// ============================================================================
// 공통
// ============================================================================

const $ = id => document.getElementById(id);

const names = {
    connectionPage: '포트·연결',
    manual: '수동·위치',
    recipes: '레시피·포인트',
    drawings: '도면 캘리브레이션',
    gamepad: '게임패드',
    settings: '설정',
    laser: '레이저 모듈',
    protocol: '프로토콜·고급 설정',
    logs: '통신 로그'
};

let status = {};
let recipes = [];
let editId = null;
let logs = [];

let lastTx = '';
let lastRx = '';

let commands = [];
let selectedCommandGroup = '';
let selectedCommandName = '';
const protocolGroupGuide = window.protocolGroupGuide || {};
const protocolCommandGuide = window.protocolCommandGuide || {};

// 레이저 전용 PTM 웹 메뉴. 주석을 해제하면 해당 분류를 다시 표시합니다.
// 서버의 명령 구현과 API 계약은 그대로 유지합니다.
const visibleProtocolGroups = new Set([
    'alarm', 'device', 'drawing', 'echo', 'home', 'laser', 'maintenance',
    'monitor', 'motion', 'motor', 'pelco', 'point', 'power_save', 'protocol',
    'ptm', 'raw', 'recipe', 'serial', 'system', 'zero',
    // 'camera',
    // 'cruise',
    // 'focus',
    // 'lens',
    // 'pattern',
    // 'preset', // 자동 운전은 point.goto의 좌표 이동을 사용합니다.
    // 'scan',
    // 'zoom',
    // 'magnification',
    // 'screen',
    // 'zone',
    // 'aux',
]);
const hiddenProtocolCommands = new Set(['home.after', 'home.time', 'pelco.flip']);

function visibleProtocolCommand(command)
{
    return visibleProtocolGroups.has(command.split('.')[0]) && !hiddenProtocolCommands.has(command);
}


// ============================================================================
// Pan / Tilt Jog 상태
// ============================================================================

let jog = null;
let jogBusy = false;
let jogStopBusy = false;


// ============================================================================
// Gamepad 상태
// ============================================================================

let padIndex = null;
let padConnected = false;
let padAutoConnect = true;
let padMapping = null;
let padMappingKey = '';
let padMappingSignature = '';
let oldPadInputs = {};

let padActive = false;
let padCentered = false;
let padCenteredSince = 0;
let padMissingSince = 0;

// 강제정지 후에는 스틱을 중앙으로 한 번 복귀시켜야
// 다시 움직일 수 있도록 하는 안전장치
let padRequireCenter = true;

let selectedPointId = null;
let recipeTestRunning = false;
let recipeTestAbort = false;
let recipeTestCommandPromise = null;
let recipeTestStopPromise = null;
let recipeTestSession = null;
let localManualOutput = false;
let recipeEstimates = new Map();
let recipeEstimateKey = '';
let recipeEstimateStatus = '활성 도면과 X/Y 캘리브레이션이 필요합니다.';
let serialSettingsLoaded = false;

let drawings = [];
let activeDrawing = null;
let drawingMesh = null;
let selectedDrawingPointId = null;
let drawingProjected = [];
let drawingFacesForPick = [];
let drawingProgressTimer = null;
let drawingRedrawFrame = null;
let drawingDragPointId = null;
let drawingActionBusy = false;
let drawingView = {
    yaw: -35,
    pitch: 22,
    zoom: 1,
    panX: 0,
    panY: 0,
    renderMode: 'clean',
    viewPreset: 'iso',
    quality: 'balanced',
    interacting: false,
    dragging: false,
    button: 0,
    moved: false,
    lastX: 0,
    lastY: 0,
    wheelTimer: null
};


// ============================================================================
// 실제 확인된 GAMEPAD RAW 매핑
// ============================================================================
//
// axes:
//
// 0 = 왼쪽 조이스틱 X
// 1 = 왼쪽 조이스틱 Y
//
// Linux/현재 패드에서 실제 확인된 축:
// 5 = 오른쪽 조이스틱 좌/우  -> PAN
// 2 = 오른쪽 조이스틱 상/하  -> TILT
//
// 9 = 방향키 POV
//
// buttons:
//
// 0  = Y
// 1  = B
// 2  = A
// 3  = X
// 4  = LB
// 5  = RB
// 6  = LT
// 7  = RT
// 8  = BACK
// 9  = NEXT
// 10 = 왼쪽 조이스틱 클릭
// 11 = 오른쪽 조이스틱 클릭
//
// ============================================================================

const PAD_AXIS = Object.freeze({

    RIGHT_X: 5,

    RIGHT_Y: 2,

    POV: 9
});


const PAD_BUTTON = Object.freeze({

    Y: 0,

    B: 1,

    A: 2,

    X: 3,

    LB: 4,

    RB: 5,

    LT: 6,

    RT: 7,

    BACK: 8,

    NEXT: 9,

    L3: 10,

    R3: 11
});


const PAD_DEADZONE = 0.15;
const PAD_CENTER_STOP_DELAY_MS = 200;


// ============================================================================
// API 기본 템플릿
// ============================================================================

const fallbackTemplates = {

    'motion.jog': {
        pan: 'right',
        tilt: 'stop',
        speed_level: 5
    },

    'motion.absolute': {
        pan: 0,
        tilt: 0
    },

    'motion.completion_config': {
        tolerance_deg: 0.2,
        stable_samples: 3,
        timeout_s: 30
    },

    'monitor.set': {
        enabled: true,
        interval_ms: 500
    },

    'serial.connect': {
        port: '',
        baudrate: 9600,
        address: 1
    },

    'serial.scan': {
        baudrates: [9600],
        first_address: 1,
        last_address: 1,
        timeout_ms: 200
    },

    'serial.auto_reconnect': {
        enabled: true,
        baudrates: [9600],
        first_address: 1,
        last_address: 16,
        timeout_ms: 200,
        retry_interval_s: 3,
        health_interval_s: 2
    },

    'recipe.upsert': {
        name: 'Recipe',
        description: ''
    },

    'recipe.delete': {
        recipe_id: ''
    },

    'point.upsert': {
        recipe_id: '',
        name: 'P1',
        pan: 0,
        tilt: 0,
        dwell_ms: 0,
        enabled: true,
        note: ''
    },

    'point.delete': {
        recipe_id: '',
        point_id: ''
    },

    'point.goto': {
        recipe_id: '',
        point_id: ''
    },

    'point.reorder': {
        recipe_id: '',
        ordered_ids: []
    },

    'drawing.get': {
        drawing_id: ''
    },

    'drawing.point_upsert': {
        drawing_id: '',
        label: 'P1',
        x: 0,
        y: 0,
        z: 0,
        calibration: false,
        pan: null,
        tilt: null
    },

    'drawing.point_delete': {
        drawing_id: '',
        point_id: ''
    },

    'drawing.point_reorder': {
        drawing_id: '',
        ordered_ids: []
    },

    'drawing.calibrate': {
        drawing_id: '',
        product_rotation_deg: 0,
        product_tilt_deg: 0
    },

    'drawing.estimate_xy': {
        drawing_id: '',
        pan: 0,
        tilt: 0
    },

    'drawing.export_recipe': {
        drawing_id: '',
        recipe_id: ''
    },

    'drawing.import_recipe': {
        drawing_id: '',
        recipe_id: ''
    },

    'scan.speed': {},

    'cruise.speed': {},

    'scan.speed_adjust': {},

    'preset.speed_adjust': {},

    'preset.goto': {
        number: 1
    },

    'preset.delete': {
        number: 1,
        confirm: true
    },

    'screen.write_char': {
        column: 0,
        char: 'A'
    },

    'zone.set_end': {
        zone: 1
    },

    'home.time': {
        value: 1
    }
};


const templates = {
    ...fallbackTemplates,
    ...(window.protocolTemplates || {})
};


// ============================================================================
// 로그
// ============================================================================

function log(kind, data)
{
    logs.unshift({

        time: new Date().toISOString(),

        kind,

        data:
            typeof data === 'string'
                ? data
                : JSON.stringify(data)
    });

    logs = logs.slice(0, 500);

    renderLogs();
}


function renderLogs()
{
    $('logRows').replaceChildren(

        ...logs.map(item => {

            const tr =
                document.createElement('tr');

            const values = [
                item.time,
                item.kind,
                item.data
            ];

            for (const value of values)
            {
                const td =
                    document.createElement('td');

                td.textContent =
                    value;

                tr.append(td);
            }

            return tr;
        })
    );
}


// ============================================================================
// HTTP API
// ============================================================================

async function request(path, body, signal)
{
    const response =
        await fetch(
            path,

            body
                ? {
                    method: 'POST',

                    headers: {
                        'Content-Type': 'application/json'
                    },

                    body:
                        JSON.stringify(body),
                    signal
                }
                : {signal}
        );


    const text =
        await response.text();

    let json;

    try
    {
        json =
            JSON.parse(
                text || '{}'
            );
    }
    catch
    {
        json =
            {
                ok: false,
                error: {
                    message:
                        text ||
                        response.statusText ||
                        '서버 응답을 해석할 수 없습니다.'
                }
            };
    }


    if (
        !response.ok ||
        json.ok === false
    )
    {
        throw Error(
            json.error?.message ||
            response.statusText
        );
    }


    return json.result ?? json;
}


async function cmd(
    command,
    params = {},
    quiet = false,
    recipeTestOwned = false
)
{
    if ((recipeTestRunning || status.operation?.test_active) && !recipeTestOwned)
    {
        const drawingMutation = command.startsWith('drawing.') && ![
            'drawing.list', 'drawing.get', 'drawing.export',
            'drawing.estimate_pan_tilt', 'drawing.estimate_xy', 'drawing.estimate_recipe'
        ].includes(command);
        if (['motion.stop', 'motion.force_stop', 'laser.off', 'serial.disconnect'].includes(command))
        {
            const stopped = await stopRecipePointTest({operator: true});
            if (command === 'motion.stop' || command === 'laser.off') return stopped;
        }
        else if ((drawingMutation || /^(motion\.|home\.|point\.(goto|upsert|delete|reorder|select)|recipe\.(create|update|upsert|delete|import|select)|laser\.(on|pulse|state|settings)|serial\.(connect|scan)|raw\.send)/.test(command))
            && !(command === 'motion.completion_config' && Object.keys(params).length === 0))
        {
            throw Error('포인트 테스트 중에는 수동 조작과 레시피 변경을 할 수 없습니다. 테스트를 먼저 정지하세요.');
        }
    }
    if (recipeTestOwned && recipeTestSession?.token && !command.startsWith('test.'))
    {
        params = {test_token: recipeTestSession.token, ...params};
    }
    if (!recipeTestOwned && /^(motion\.(absolute|relative|goto|jog)$|point\.goto$|home\.start$|cruise\.start$|scan\.start$|laser\.(on|pulse|state)$|raw\.send$)/.test(command))
    {
        localManualOutput = true;
    }
    if (['drawing.point_upsert', 'drawing.calibrate', 'drawing.reset_calibration',
         'drawing.estimate_pan_tilt', 'drawing.estimate_xy'].includes(command) &&
        params.drawing_id === activeDrawing?.id && activeDrawing?.selected_recipe_id)
    {
        params = {recipe_id: activeDrawing.selected_recipe_id, ...params};
    }
    try
    {
        const result =
            await request(
                '/api/command',

                {
                    command,
                    params
                },
                recipeTestOwned ? (command === 'test.stop'
                    ? recipeTestSession?.stopController?.signal : recipeTestSession?.controller.signal) : undefined
            );


        if (!quiet)
        {
            log(
                command,
                result
            );

            if (!drawingActionBusy)
            {
                $('notice').dataset.kind = 'success';
                $('notice').textContent = command + ' 완료';
            }
        }


        return result;
    }
    catch (error)
    {
        if (recipeTestOwned && recipeTestSession?.error) error = recipeTestSession.error;
        $('notice').dataset.kind = 'error';
        $('notice').textContent =
            error.message;

        log(
            '오류',
            command + ': ' + error.message
        );

        throw error;
    }
}


function guarded(fn)
{
    return (...args) => {

        try
        {
            return Promise
                .resolve(
                    fn(...args)
                )
                .catch(error => {
                    $('notice').dataset.kind = 'error';
                    $('notice').textContent =
                        error.message;
                });
        }
        catch (error)
        {
            $('notice').dataset.kind = 'error';
            $('notice').textContent =
                error.message;
        }
    };
}


function drawingAction(fn, options)
{
    return guarded(async event => {
        if (drawingActionBusy) return;
        const feedback = typeof options === 'function' ? options() : options;
        const trigger = feedback.buttonId ? $(feedback.buttonId) : event?.currentTarget;
        const button = trigger?.tagName === 'BUTTON' ? trigger : null;
        const originalText = button?.textContent;
        const controls = Array.from(document.querySelectorAll(
            '#drawings .drawing-calibration-actions button, #drawingPoints input, #drawingPointSets input, ' +
            '#loadDrawing, #manageDrawings, #drawingSelect, #drawingActiveRecipe, #newDrawingRecipe, ' +
            '#drawingProductRotation, #drawingProductTilt, #drawingRecipeDialog button, ' +
            '#drawingRecipeDialog input, #drawingRecipeDialog select, #drawingManagerDialog button, #drawingManagerDialog input'));
        const disabled = controls.map(control => [control, control.disabled]);
        const show = (message, kind) => {
            for (const element of [$('notice'), $(feedback.statusId || 'drawingStatus')])
            {
                if (!element) continue;
                element.textContent = message;
                element.dataset.kind = kind;
                element.setAttribute('aria-busy', String(kind === 'busy'));
            }
        };
        drawingActionBusy = true;
        for (const control of controls) control.disabled = true;
        if (button)
        {
            button.textContent = feedback.pending;
            button.setAttribute('aria-busy', 'true');
        }
        show(feedback.pending, 'busy');
        try
        {
            const result = await fn(event);
            show(result === false ? '작업을 취소했습니다.' : feedback.done, result === false ? 'info' : 'success');
            return result;
        }
        catch (error)
        {
            show(`작업 실패: ${error.message}`, 'error');
            throw error;
        }
        finally
        {
            drawingActionBusy = false;
            for (const [control, value] of disabled) control.disabled = value;
            if (button)
            {
                if (button.textContent === feedback.pending) button.textContent = originalText;
                button.removeAttribute('aria-busy');
            }
            renderDrawingRecipeSelector();
        }
    });
}


// ============================================================================
// 버튼 생성
// ============================================================================

function button(
    text,
    fn,
    title = text
)
{
    const element =
        document.createElement('button');


    element.type =
        'button';

    element.textContent =
        text;

    element.title =
        title;

    element.onclick =
        guarded(fn);


    return element;
}


// ============================================================================
// TAB
// ============================================================================

for (
    const [id, label]
    of Object.entries(names)
)
{
    if (!$(id))
    {
        continue;
    }

    const tab =
        button(
            label,

            () => {

                for (
                    const key
                    of Object.keys(names)
                )
                {
                    const page = $(key);
                    if (page)
                    {
                        page.hidden = key !== id;
                    }
                }


                if (id === 'settings') guarded(loadPtmCommunicationSettings)();
                if (id === 'drawings')
                {
                    window.dispatchEvent(
                        new CustomEvent('drawing-tab-visible')
                    );
                }


                for (
                    const item
                    of $('tabs').children
                )
                {
                    item.setAttribute(
                        'aria-selected',
                        String(item === tab)
                    );
                }
            }
        );


    tab.setAttribute(
        'aria-selected',
        String(id === 'manual')
    );


    $('tabs').append(tab);
}


// ============================================================================
// Pan / Tilt 속도
// ============================================================================

const speedPercent = [
    1,
    3,
    8,
    15,
    30,
    50,
    70,
    100
];


for (const id of ['speedLevel'])
{
    speedPercent.forEach(
        (percent, index) => {

            $(id).add(
                new Option(
                    `${index + 1}단계 (${percent}%)`,
                    index + 1
                )
            );
        }
    );


    $(id).value =
        localStorage.getItem(id)
        || localStorage.getItem('panLevel')
        || '5';


    $(id).onchange =
        () => {

            localStorage.setItem(
                id,
                $(id).value
            );
            updateDrawingSpeedLevel();
        };
}


function updateDrawingSpeedLevel()
{
    const output = $('drawingSpeedLevel');
    const level = Number($('speedLevel')?.value || 5);
    if (output)
    {
        output.textContent = `수동 속도 ${level}단계 (${speedPercent[level - 1]}%)`;
    }
}


updateDrawingSpeedLevel();


// ============================================================================
// Form
// ============================================================================

function formData(form)
{
    const data = {};


    for (
        const element
        of form.elements
    )
    {
        if (!element.name)
        {
            continue;
        }


        if (element.type === 'checkbox')
        {
            data[element.name] =
                element.checked;
        }

        else if (
            element.type === 'number'
        )
        {
            data[element.name] =
                Number(element.value);
        }

        else
        {
            data[element.name] =
                element.value;
        }
    }


    return data;
}


function bindForm(
    id,
    command
)
{
    const form = $(id);
    if (!form)
    {
        return;
    }
    form.onsubmit =
        guarded(
            async event => {

                event.preventDefault();


                await cmd(
                    command,
                    formData(event.target)
                );
            }
        );
}


bindForm(
    'absolute',
    'motion.absolute'
);

bindForm(
    'monitor',
    'monitor.set'
);

bindForm(
    'completion',
    'motion.completion_config'
);



// ============================================================================
// data-command 버튼
// ============================================================================

document
    .querySelectorAll('[data-command]')
    .forEach(element => {

        element.onclick =
            guarded(
                () => cmd(
                    element.dataset.command,

                    JSON.parse(
                        element.dataset.params || '{}'
                    )
                )
            );
    });


// ============================================================================
// Serial Port
// ============================================================================

async function ports()
{
    const [data, saved] =
        await Promise.all([
            request('/api/ports'),
            request('/api/status')
        ]);


    const current =
        $('port').value ||
        saved.serial?.configured_port ||
        '';


    const options =
        data.ports.map(
            item => new Option(
                `${item.port} — ${item.description}`,
                item.port
            )
        );


    if (
        current &&
        !data.ports.some(item => item.port === current)
    )
    {
        options.unshift(
            new Option(`${current} — 저장된 포트`, current)
        );
    }


    $('port').replaceChildren(
        ...options
    );


    if (
        [...$('port').options]
            .some(
                option =>
                    option.value === current
            )
    )
    {
        $('port').value =
            current;
    }


    if (!serialSettingsLoaded)
    {
        $('baud').value =
            String(saved.serial?.baudrate || 9600);

        $('address').value =
            String(saved.serial?.address || 1);

        serialSettingsLoaded = true;
    }
}


async function savePortSettings()
{
    return cmd(
        'serial.settings',
        {
            port: $('port').value,
            baudrate: Number($('baud').value),
            address: Number($('address').value)
        }
    );
}


$('ports').onclick =
    guarded(ports);


$('savePortSettings').onclick =
    guarded(savePortSettings);

let laserSettingsDirty = false;
let laserProfilesKey = '';
const laserStateNames = {
    point_irradiation: '1. 포인트 조사',
    position_check: '2. 체결점 위치 확인',
    wrong_point: '3. 잘못된 체결점',
    work_complete: '4. 작업 완료'
};
function renderLaserStateProfiles(laser)
{
    $('laserWorkflowStatus').textContent = `현재 상태: ${laserStateNames[laser.active_state] || '소등/대기'}`;
    if (!laser.state_profiles)
    {
        $('laserStateProfiles').textContent = '상태별 조사 기능을 사용하려면 PTM 서버를 최신 버전으로 재시작하세요.';
        return;
    }
    const key = JSON.stringify(laser.state_profiles);
    if (laserSettingsDirty || key === laserProfilesKey) return;
    laserProfilesKey = key;
    $('laserStateProfiles').replaceChildren(...Object.entries(laserStateNames).map(([state, name]) => {
        const row = document.createElement('tr');
        row.dataset.state = state;
        const select = document.createElement('select');
        select.setAttribute('aria-label', `${name} 조사 방식`);
        select.replaceChildren(new Option('점등', 'on'), new Option('점멸', 'blink'), new Option('소등', 'off'));
        select.value = laser.state_profiles[state].mode;
        const interval = document.createElement('input');
        interval.type = 'number';
        interval.min = '100'; interval.max = '60000'; interval.step = '10';
        interval.setAttribute('aria-label', `${name} 점멸 간격`);
        interval.value = laser.state_profiles[state].interval_ms;
        interval.disabled = select.value !== 'blink';
        select.oninput = () => {laserSettingsDirty = true; interval.disabled = select.value !== 'blink';};
        interval.oninput = () => {laserSettingsDirty = true;};
        const trial = document.createElement('button');
        trial.type = 'button'; trial.textContent = '시험';
        trial.onclick = guarded(async () => {
            await saveLaserSettings();
            await cmd('laser.state', {state, manual: true});
        });
        for (const element of [name, select, interval, trial])
        {
            const cell = document.createElement('td');
            if (typeof element === 'string') cell.textContent = element;
            else cell.append(element);
            row.append(cell);
        }
        return row;
    }));
}

async function saveLaserSettings()
{
    const profiles = {};
    for (const row of $('laserStateProfiles').querySelectorAll('tr[data-state]'))
    {
        const interval = Number(row.querySelector('input').value);
        if (!Number.isInteger(interval) || interval < 100 || interval > 60000) throw Error('점멸 간격은 100~60000 ms 정수로 입력하세요.');
        profiles[row.dataset.state] = {mode: row.querySelector('select').value, interval_ms: interval};
    }
    await cmd('laser.settings', {bypass: $('laserBypass').checked,
        control_priority: $('laserControlPriority').checked,
        ...(Object.keys(profiles).length ? {state_profiles: profiles} : {})});
    laserSettingsDirty = false;
}
$('laserBypass').oninput = () => { laserSettingsDirty = true; };
$('laserControlPriority').onchange = guarded(async () => {
    const checkbox = $('laserControlPriority');
    checkbox.disabled = true;
    try { await cmd('laser.settings', {control_priority: checkbox.checked}); }
    finally { checkbox.disabled = false; }
});
$('saveLaserSettings').onclick = guarded(saveLaserSettings);

let ptmSettingsDirty = false;

function renderPtmSettings(settings)
{
    if (!settings) return;
    for (const [key, settingId] of [
        ['invert_pan', 'settingInvertPan'],
        ['invert_tilt', 'settingInvertTilt']
    ])
    {
        if (!ptmSettingsDirty) $(settingId).checked = Boolean(settings[key]);
    }
    if (!ptmSettingsDirty) $('settingPowerOnHome').value = settings.power_on_home === null ? '' : String(settings.power_on_home);
    $('ptmSettingsState').textContent = settings.power_on_home_error
        ? '저장됨 / 전원 ON 호밍 설정 전송 실패: ' + settings.power_on_home_error
        : settings.power_on_home === null ? '서버에 저장됨 / 전원 ON 호밍 미설정'
        : settings.power_on_home_sent ? '서버에 저장됨 / 호밍 설정 명령 전송됨 (장치 피드백 없음)'
        : '서버에 저장됨 / 다음 장치 연결 시 호밍 설정 전송';
}

$('ptmSettings').oninput = () => { ptmSettingsDirty = true; };
$('ptmSettings').onsubmit = guarded(async event => {
    event.preventDefault();
    const saved = await cmd('ptm.settings', {
        invert_pan: $('settingInvertPan').checked,
        invert_tilt: $('settingInvertTilt').checked,
        power_on_home: $('settingPowerOnHome').value === '' ? null : $('settingPowerOnHome').value === 'true',
        confirm: true
    });
    ptmSettingsDirty = false;
    status.settings = saved;
    renderPtmSettings(saved);
});

let ptmCommunicationSettings = null;
let ptmCommunicationBusy = false;

function renderPtmCommunicationSettings(settings)
{
    ptmCommunicationSettings = settings;
    $('ptmStatusTimeout').value = settings.timeout_s;
    $('ptmStatusRetries').value = settings.status_retries;
    $('ptmCommunicationState').textContent = 'Core에 저장됨';
}

function setPtmCommunicationBusy(busy)
{
    ptmCommunicationBusy = busy;
    $('refreshPtmCommunication').disabled = busy;
    for (const input of $('ptmCommunicationSettings').querySelectorAll('input, button'))
        input.disabled = busy || !ptmCommunicationSettings;
}

async function loadPtmCommunicationSettings()
{
    if (ptmCommunicationBusy) return;
    setPtmCommunicationBusy(true);
    $('ptmCommunicationState').textContent = 'Core 설정 조회 중…';
    try
    {
        renderPtmCommunicationSettings(await cmd('ptm.communication_settings', {}, true));
    }
    catch (error)
    {
        ptmCommunicationSettings = null;
        $('ptmCommunicationState').textContent = '조회 실패: ' + error.message;
        throw error;
    }
    finally { setPtmCommunicationBusy(false); }
}

$('refreshPtmCommunication').onclick = guarded(loadPtmCommunicationSettings);
$('ptmCommunicationSettings').onsubmit = guarded(async event => {
    event.preventDefault();
    if (ptmCommunicationBusy || !ptmCommunicationSettings) return;
    const params = {
        timeout_s: Number($('ptmStatusTimeout').value),
        status_retries: Number($('ptmStatusRetries').value),
        expected_timeout_s: ptmCommunicationSettings.timeout_s,
        expected_status_retries: ptmCommunicationSettings.status_retries
    };
    setPtmCommunicationBusy(true);
    $('ptmCommunicationState').textContent = 'Core에 저장 중…';
    try
    {
        renderPtmCommunicationSettings(await cmd('ptm.communication_settings', params));
    }
    catch (error)
    {
        $('ptmCommunicationState').textContent = '저장 실패: ' + error.message;
        throw error;
    }
    finally { setPtmCommunicationBusy(false); }
});


$('connect').onclick =
    guarded(
        async () => {
            await savePortSettings();
            return cmd(
                'serial.connect',

                {
                    port:
                        $('port').value,

                    baudrate:
                        Number(
                            $('baud').value
                        ),

                    address:
                        Number(
                            $('address').value
                        )
                }
            );
        }
    );


$('disconnect').onclick =
    guarded(
        async () => {

            await stopJog();

            await cmd(
                'serial.disconnect'
            );
        }
    );


$('scan').onclick =
    guarded(
        () => cmd(
            'serial.scan',

            {
                baudrates: [
                    9600
                ],

                first_address:
                    Number(
                        $('address').value
                    ),

                last_address:
                    Number(
                        $('address').value
                    ),

                timeout_ms: 200
            }
        )
    );


$('scanCancel').onclick =
    guarded(
        () => cmd(
            'serial.scan_cancel'
        )
    );


// ============================================================================
// Pan / Tilt STOP
// ============================================================================

async function stopJog(preserveLaserManual = false)
{
    jog =
        null;

    padActive =
        false;

    padCentered =
        false;

    padCenteredSince =
        0;

    padMissingSince =
        0;

    // 스틱을 중앙으로 다시 놓기 전까지
    // 재동작 방지
    padRequireCenter =
        true;

    if (recipeTestRunning || status.operation?.test_active) return stopRecipePointTest({operator: true});

    // A JOG POST may already be in flight.  Let it finish first so an older
    // motion request cannot arrive at the server after STOP.
    while (jogBusy)
    {
        await new Promise(
            resolve =>
                setTimeout(
                    resolve,
                    20
                )
        );
    }


    if (
        !status.serial?.connected ||
        jogStopBusy
    )
    {
        return;
    }


    // Multiple release/cancel paths may arrive close together. Do not enqueue
    // duplicate STOP requests for the same release.
    jogStopBusy =
        true;

    try
    {
        await cmd(
            'motion.stop',
            {preserve_laser_manual: preserveLaserManual === true},
            true
        );
    }
    finally
    {
        jogStopBusy =
            false;
    }
}


$('stopAll').onclick =
    guarded(stopJog);


$('dialStop').onclick =
    guarded(stopJog);


// ============================================================================
// 웹 수동 Pan/Tilt 버튼
// ============================================================================

document
    .querySelectorAll('[data-pan]')
    .forEach(element => {

        // Manual control is a held ON state. Prevent browser touch/drag gestures
        // from cancelling the pointer while the operator is still holding it.
        element.style.touchAction = 'none';
        element.style.userSelect = 'none';

        element.onpointerdown =
            guarded(
                event => {

                    event.preventDefault();

                    if (
                        !status.serial?.connected || recipeTestRunning || status.operation?.test_active
                    )
                    {
                        return;
                    }


                    element.setPointerCapture(
                        event.pointerId
                    );


                    jog = {

                        pan: element.dataset.pan,
                        tilt: element.dataset.tilt
                    };


                    return sendJog();
                }
            );


        // Only a real button/key OFF stops normal manual movement. A browser
        // pointercancel is not treated as OFF because it can occur from gesture
        // arbitration even while the operator is still holding the control.
        element.addEventListener(
            'pointerup',
            guarded(() => { if ((!recipeTestRunning && !status.operation?.test_active) || jog) return stopJog(true); })
        );
    });


// ============================================================================
// JOG 송신
// ============================================================================

async function sendJog()
{
    if (
        !jog ||
        jogBusy
    )
    {
        return;
    }


    // Serialize a new JOG behind an earlier STOP request. This prevents a rapid
    // release/re-press from being reordered by concurrent HTTP requests.
    while (jogStopBusy)
    {
        await new Promise(
            resolve =>
                setTimeout(
                    resolve,
                    10
                )
        );

        if (!jog)
        {
            return;
        }
    }


    jogBusy =
        true;


    try
    {
        await cmd(
            'motion.jog',

            {
                ...jog,

                speed_level:
                    Number(
                        $('speedLevel').value
                    )
            },

            true
        );
    }

    catch
    {
        jog =
            null;

        padActive =
            false;
    }

    finally
    {
        jogBusy =
            false;
    }
}


// Manual JOG is intentionally not stopped merely because the browser loses
// focus or becomes hidden.  The operator release/STOP state is authoritative.
// pagehide below still sends a final safety STOP when the page is actually left.

window.addEventListener(
    'pagehide',

    () => {

        recipeTestAbort = true;

        if (recipeTestSession?.token)
        {
            navigator.sendBeacon('/api/command', new Blob(
                [JSON.stringify({command: 'test.stop', params: {test_token: recipeTestSession.token}})],
                {type: 'application/json'}
            ));
            return;
        }
        if (status.operation?.test_active || !localManualOutput) return;

        navigator.sendBeacon(

            '/api/command',

            new Blob(
                [
                    JSON.stringify({

                        command:
                            'motion.stop',

                        params: {if_no_test_session: true}
                    })
                ],

                {
                    type:
                        'application/json'
                }
            )
        );
        navigator.sendBeacon('/api/command', new Blob(
            [JSON.stringify({command: 'laser.off', params: {if_no_test_session: true}})],
            {type: 'application/json'}
        ));
    }
);


// ============================================================================
// 상태 조회
// ============================================================================

let refreshTimer = null;
let refreshPromise = null;


// 상태를 딱 한 번만 조회한다.
// 순차 테스트에서는 이 함수를 사용해서 자동 갱신 타이머가 늘어나지 않게 한다.
async function refreshOnce()
{
    // 이미 /api/status 요청이 진행 중이면 같은 요청을 같이 기다린다.
    // 백그라운드 갱신과 순차 테스트 갱신이 겹쳐 응답 순서가 뒤섞이는 것을 방지한다.
    if (refreshPromise)
    {
        return refreshPromise;
    }


    refreshPromise =
        (async () => {
            try
            {
                status =
                    await request(
                        '/api/status'
                    );
                updateRecipeTestButtons();

                $('connection').classList.toggle('connected', Boolean(status.serial?.connected) &&
                    !['communication_error', 'communication_timeout'].includes(status.motion?.state));


                if (
                    status.serial.connected
                )
                {
                    $('connection').textContent =
                        `${status.serial.port} / ` +
                        `${status.serial.baudrate} / ` +
                        `주소 ${status.serial.address}`;
                }

                else if (
                    status.scanning
                )
                {
                    $('connection').textContent =
                        '탐색 중';
                }

                else
                {
                    $('connection').textContent =
                        '연결 안 됨';
                }


                $('actualPan').textContent =
                    status.position.pan === null
                        ? '—'
                        : status.position.pan
                            .toFixed(2)
                            + '°';


                $('actualTilt').textContent =
                    status.position.tilt === null
                        ? '—'
                        : status.position.tilt
                            .toFixed(2)
                            + '°';


                $('motionState').textContent =
                    status.motion.state
                    || 'idle';

                renderPtmSettings(status.settings);


                if ($('laserState'))
                {
                    renderLaserStateProfiles(status.laser);
                    if (!laserSettingsDirty)
                    {
                        $('laserBypass').checked = Boolean(status.laser.bypass);
                    }
                    if (!$('laserControlPriority').disabled)
                    {
                        $('laserControlPriority').checked = Boolean(status.laser.control_priority);
                    }
                    $('laserBypassState').textContent = status.laser.bypass ? '바이패스 / 출력 차단' : (status.laser.backend === 'gpio' ? 'GPIO BOARD 31' : 'RS485');
                    $('laserState').textContent = 'GPIO 명령: ' +
                        (
                            status.laser.on
                                ? 'ON'
                                : 'OFF'
                        ) + (status.laser.manual_override ? ` / 수동 ${Math.ceil(status.laser.manual_remaining_s)}초 남음` : ' / 자동')
                        + (status.laser.error ? ` / GPIO 오류: ${status.laser.error}` : '');
                }


                if (
                    status.last_tx &&
                    JSON.stringify(
                        status.last_tx
                    ) !== lastTx
                )
                {
                    lastTx =
                        JSON.stringify(
                            status.last_tx
                        );


                    log(
                        'TX',
                        status.last_tx
                    );
                }


                const rx =
                    JSON.stringify(
                        status.last_rx
                    );


                if (
                    rx !== lastRx &&
                    status.last_rx.length
                )
                {
                    lastRx =
                        rx;


                    log(
                        'RX',
                        status.last_rx
                    );
                }


                $('scan').disabled =
                    status.scanning ||
                    status.serial.connected;


                $('connect').disabled =
                    status.scanning ||
                    status.serial.connected;


                $('disconnect').disabled =
                    !status.serial.connected;
            }

            catch
            {
                $('connection').classList.remove('connected');
                $('connection').textContent =
                    '서버 연결 끊김';


                status =
                    {};
                updateRecipeTestButtons();


                jog =
                    null;
            }
        })();


    try
    {
        return await refreshPromise;
    }

    finally
    {
        refreshPromise =
            null;
    }
}


// 화면 상태 갱신용 단일 루프.
// 여러 곳에서 refresh()가 호출되더라도 타이머는 항상 하나만 유지한다.
function scheduleRefresh()
{
    if (refreshTimer)
    {
        clearTimeout(
            refreshTimer
        );
    }


    refreshTimer =
        setTimeout(
            () => {
                refreshTimer =
                    null;


                refresh();
            },
            700
        );
}


async function refresh()
{
    try
    {
        await refreshOnce();
    }

    finally
    {
        scheduleRefresh();
    }
}


// ============================================================================
// Recipe
// ============================================================================

async function loadRecipes(select)
{
    recipes =
        (
            await cmd(
                'recipe.list',
                {},
                true
            )
        ).recipes;


    const id =
        select ||
        $('recipeSelect').value;


    $('recipeSelect').replaceChildren(

        ...recipes.map(
            recipe =>
                new Option(
                    `ID ${recipe.index} · ${recipe.name}`,
                    recipe.id
                )
        )
    );


    if (
        recipes.some(
            recipe =>
                recipe.id === id
        )
    )
    {
        $('recipeSelect').value =
            id;
    }


    renderPoints();
    if (activeDrawing?.selected_recipe_id &&
        !recipes.some(recipe => recipe.id === activeDrawing.selected_recipe_id))
    {
        activeDrawing.selected_recipe_id = '';
    }
    renderDrawingRecipeSelector();
}


function selected()
{
    return recipes.find(
        recipe =>
            recipe.id ===
            $('recipeSelect').value
    );
}


function selectedPoint()
{
    const recipe =
        selected();


    const points =
        recipe?.points || [];


    if (
        !points.some(
            point =>
                point.id ===
                selectedPointId
        )
    )
    {
        selectedPointId =
            points[0]?.id
            || null;
    }


    return points.find(
        point =>
            point.id ===
            selectedPointId
    ) || null;
}


function requestRecipeEstimates()
{
    const recipe = selected();
    const drawing = activeDrawing;

    if (!recipe || !drawing?.calibration)
    {
        recipeEstimates = new Map();
        recipeEstimateKey = '';
        recipeEstimateStatus = drawing
            ? '활성 도면의 X/Y 캘리브레이션이 필요합니다.'
            : '활성 도면과 X/Y 캘리브레이션이 필요합니다.';
        return;
    }

    const key =
        `${drawing.id}:${drawing.updated}:${recipe.id}:${recipe.updated}`;

    if (key === recipeEstimateKey)
    {
        return;
    }

    recipeEstimateKey = key;
    recipeEstimates = new Map();
    recipeEstimateStatus = '추정값 계산 중…';

    cmd(
        'drawing.estimate_recipe',
        {
            drawing_id: drawing.id,
            recipe_id: recipe.id
        },
        true
    )
        .then(result => {
            if (recipeEstimateKey !== key)
            {
                return;
            }
            recipeEstimates = new Map(
                result.estimates.map(item => [item.point_id, item])
            );
            recipeEstimateStatus = '';
            renderPoints();
        })
        .catch(error => {
            if (recipeEstimateKey !== key)
            {
                return;
            }
            recipeEstimateStatus = error.message;
            renderPoints();
        });
}


function enabledRecipePoints(recipe = selected())
{
    return (recipe?.points || [])
        .filter(point => point.enabled)
        .sort((left, right) => Number(left.order) - Number(right.order));
}


function pointLabel(point)
{
    if (!point)
    {
        return '-';
    }


    return `#${point.order} ${point.name} · Pan ${Number(point.pan).toFixed(2)}° / Tilt ${Number(point.tilt).toFixed(2)}°`;
}


function updateSelectedPointStatus()
{
    const point =
        selectedPoint();


    const element =
        $('selectedPointStatus');


    if (element)
    {
        element.textContent =
            '선택 포인트: '
            + pointLabel(point);
    }
}


function setRecipeTestStatus(text)
{
    const element =
        $('recipeTestStatus');


    if (element)
    {
        element.textContent =
            text;
    }
}


function updateRecipeTestButtons()
{
    const busy = recipeTestRunning || status.operation?.test_active === true;
    for (
        const id
        of [
            'runRecipeSequence',
            'runRecipeRandom',
            'recipeTestLaserMs',
            'recipeSelect'
        ]
    )
    {
        if ($(id))
        {
            $(id).disabled =
                busy;
        }
    }


    if ($('stopRecipeTest'))
    {
        $('stopRecipeTest').disabled =
            !busy;
    }
}


function sleep(ms)
{
    return new Promise(
        resolve =>
            setTimeout(
                resolve,
                ms
            )
    );
}


async function waitForRecipeMotion(motionId)
{
    const completion =
        status.motion?.completion || {};


    const timeoutMs =
        Math.max(
            3000,
            Number(completion.timeout_s || 30) * 1000 + 1000
        );


    const deadline =
        Date.now() + timeoutMs;


    while (
        !recipeTestAbort &&
        Date.now() < deadline
    )
    {
        // 순차 테스트에서는 단발 조회만 수행한다.
        // refresh()를 호출하면 700ms 자동 갱신 타이머가 계속 추가될 수 있으므로
        // 반드시 refreshOnce()를 사용한다.
        await refreshOnce();

        if (recipeTestAbort) return;
        const motion = status.motion;
        if (!motion) throw Error('이동 상태를 확인할 수 없습니다.');
        if (motion.failed_id === motionId || ['fault', 'timeout', 'stopped', 'jog', 'homing', 'communication_error', 'communication_timeout'].includes(motion.state))
        {
            throw Error(motion.last_error || '포인트 이동이 중단되었습니다.');
        }


        if (motion.id && motion.id !== motionId)
        {
            throw Error('다른 이동 명령이 포인트 테스트를 대체했습니다.');
        }
        if (
            motion.completed_id === motionId
        )
        {
            break;
        }
        await sleep(50);
    }

    if (recipeTestAbort) return;
    if (status.motion?.completed_id !== motionId)
    {
        throw Error('포인트 이동 완료 확인 시간이 초과되었습니다.');
    }


}


function clearRecipeTestTimers(session)
{
    clearTimeout(session.renewTimer);
    clearTimeout(session.expiryTimer);
}


function failRecipeTestSession(session, error)
{
    if (recipeTestSession !== session || session.stopped || session.error) return;
    session.error = error;
    recipeTestAbort = true;
    clearRecipeTestTimers(session);
    session.controller.abort();
}


function keepRecipeTestSession(session, leaseMs)
{
    clearRecipeTestTimers(session);
    session.expiryTimer = setTimeout(() => failRecipeTestSession(session,
        Error('테스트 연결 유지 시간이 초과되었습니다. 장비가 자동 정지합니다.')), leaseMs);
    session.renewTimer = setTimeout(async () => {
        if (recipeTestSession !== session || session.closing) return;
        try
        {
            const renewed = await cmd('test.renew', {test_token: session.token}, true, true);
            if (recipeTestSession !== session || session.closing) return;
            if (renewed.accepted === false) throw Error('테스트 세션이 종료되었습니다.');
            keepRecipeTestSession(session, Number(renewed.lease_ms) || leaseMs);
        }
        catch (error)
        {
            if (!session.closing) failRecipeTestSession(session,
                Error('테스트 연결 유지 실패: ' + error.message));
        }
    }, Math.min(1000, Math.max(100, Math.floor(leaseMs / 3))));
}


async function recipeTestCommand(command, params = {}, quiet = false)
{
    const session = recipeTestSession;
    if (session?.token && !command.startsWith('test.')) params = {test_token: session.token, ...params};
    const pending = cmd(command, params, quiet, true);
    recipeTestCommandPromise = pending;
    try
    {
        const result = await pending;
        if (command === 'test.start' && session === recipeTestSession && result.test_token)
        {
            session.token = result.test_token;
        }
        return result;
    }
    finally
    {
        if (recipeTestCommandPromise === pending) recipeTestCommandPromise = null;
    }
}


async function moveRecipeTestPoint(recipe, point, label, laserDurationMs, expectedRecipeRevision)
{
    selectedPointId =
        point.id;


    renderPoints();


    setRecipeTestStatus(label + ' · ' + pointLabel(point));

    const off = await recipeTestCommand('laser.off', {}, true);
    if (off.laser_on !== false) throw Error('레이저 OFF를 확인하지 못했습니다.');
    if (recipeTestAbort) return;

    const result = await recipeTestCommand(
        'point.goto',

        {
            recipe_id:
                recipe.id,

            point_id:
                point.id,
            laser_after_move: false,
            exclusive: true,
            laser_permit_token: off.permit_token,
            expected_recipe_revision: expectedRecipeRevision ?? recipe.revision
        }
    );


    if (!result.motion_id) throw Error('포인트 이동 ID를 받지 못했습니다.');
    await waitForRecipeMotion(result.motion_id);
    if (recipeTestAbort) return;

    try
    {
        // 서버 타이머로 브라우저 연결이 끊겨도 조사 시간이 지나면 소등한다.
        const laser = await recipeTestCommand('laser.pulse', {
            duration_ms: laserDurationMs,
            expected_motion_id: result.motion_id,
            expected_recipe_revision: result.recipe_revision ?? recipe.revision,
            laser_permit_token: result.laser_permit_token,
            recipe_id: recipe.id,
            point_id: point.id
        }, true);
        if (laser.laser_on !== true) throw Error('레이저 ON을 확인하지 못했습니다.');
        setRecipeTestStatus(label + ' · ' + pointLabel(point) + ` · 레이저 ON ${laserDurationMs} ms`);
        const deadline = Date.now() + laserDurationMs;
        let nextRefresh = Date.now() + 200;
        while (!recipeTestAbort && Date.now() < deadline)
        {
            await sleep(Math.min(50, deadline - Date.now()));
            if (recipeTestAbort) break;
            if (Date.now() >= nextRefresh)
            {
                await refreshOnce();
                nextRefresh = Date.now() + 200;
            }
            const motion = status.motion;
            if (!motion || ['fault', 'timeout', 'stopped', 'jog', 'homing', 'communication_error', 'communication_timeout'].includes(motion.state)
                || motion.failed_id === result.motion_id
                || (motion.id && motion.id !== result.motion_id)
                || motion.completed_id !== result.motion_id)
            {
                throw Error('이동 상태가 변경되어 포인트 테스트를 중단합니다.');
            }
            if (status.laser?.manual_allowed === false)
            {
                throw Error('레이저 사용 차단: ' + (status.laser.manual_block_reason || 'PLC 모드 또는 인터록이 변경되었습니다.'));
            }
            if (result.recipe_revision != null && status.selected_recipe_revision !== result.recipe_revision)
            {
                throw Error('레시피가 변경되어 포인트 테스트를 중단합니다.');
            }
        }
    }
    finally
    {
        const stopped = await recipeTestCommand('laser.off', {}, true);
        if (stopped.laser_on !== false) throw Error('레이저 OFF를 확인하지 못했습니다.');
    }
    return result.recipe_revision;
}


async function runRecipeSequenceTest()
{
    await runRecipePointTest('sequence');
}


async function runRecipeRandomTest()
{
    await runRecipePointTest('random');
}


async function runRecipePointTest(mode)
{
    if (recipeTestRunning)
    {
        return;
    }

    if (jog || jogBusy || jogStopBusy || padActive)
    {
        throw Error('수동 운전을 정지한 후 테스트를 시작하세요.');
    }
    if (status.operation?.test_active) throw Error('다른 포인트 테스트가 진행 중입니다.');


    const recipe =
        selected();


    const points =
        enabledRecipePoints(recipe);


    if (
        !recipe ||
        !points.length
    )
    {
        throw Error(
            '사용 가능한 레시피 포인트가 없습니다.'
        );
    }

    const laserDurationMs = Number($('recipeTestLaserMs').value);
    if (!Number.isInteger(laserDurationMs) || laserDurationMs < 50 || laserDurationMs > 60000)
    {
        throw Error('조사 시간은 50~60000 ms 범위의 정수로 입력하세요.');
    }

    recipeTestRunning =
        true;

    recipeTestAbort =
        false;

    const session = {token: null, controller: new AbortController(), closing: false, stopped: false};
    recipeTestSession = session;
    session.expiryTimer = setTimeout(() => failRecipeTestSession(session,
        Error('테스트 시작 응답 시간이 초과되었습니다.')), 5000);

    updateRecipeTestButtons();

    let finalStatus =
        mode === 'sequence'
            ? '순차 테스트 완료'
            : '랜덤 반복 정지';
    let testFailed = false;
    let testRecipeRevision;


    try
    {
        const started = await recipeTestCommand('test.start', {mode, recipe_id: recipe.id}, true);
        if (!session.token) throw Error('테스트 세션을 받지 못했습니다.');
        if (recipeTestAbort) return;
        keepRecipeTestSession(session, Number(started.lease_ms) || 5000);
        testRecipeRevision = started.recipe_revision;
        if (recipe.product_rotation_deg || recipe.product_tilt_deg)
        {
            await recipeTestCommand('recipe.pose_preview', {recipe_id: recipe.id}, true);
        }
        if (recipeTestAbort) return;
        const testStatus = await cmd('system.status', {}, true);
        if (recipeTestAbort) return;
        if (testStatus.serial?.connected !== true || !testStatus.motion || testStatus.motion.id
            || ['tracking', 'jog', 'homing'].includes(testStatus.motion?.state))
        {
            throw Error('장치 연결과 진행 중인 이동을 확인한 후 테스트를 시작하세요.');
        }
        if (testStatus.laser?.manual_allowed === false)
        {
            throw Error('레이저 사용 차단: ' + (testStatus.laser.manual_block_reason
                || 'Core의 현재 알람, 강제정지, 체결 후 재점등 제한을 확인하세요.'));
        }
        if (
            mode === 'sequence'
        )
        {
            for (
                let index = 0;
                index < points.length && !recipeTestAbort;
                index += 1
            )
            {
                testRecipeRevision = await moveRecipeTestPoint(
                    recipe,
                    points[index],
                    `순차 ${index + 1}/${points.length}`,
                    laserDurationMs,
                    testRecipeRevision
                );
            }
        }

        else
        {
            let previous = -1;
            let count = 0;


            while (!recipeTestAbort)
            {
                let index =
                    Math.floor(
                        Math.random() * points.length
                    );


                if (
                    points.length > 1 &&
                    index === previous
                )
                {
                    index =
                        (
                            index + 1
                        ) % points.length;
                }


                previous =
                    index;

                count += 1;


                testRecipeRevision = await moveRecipeTestPoint(
                    recipe,
                    points[index],
                    `랜덤 ${count}`,
                    laserDurationMs,
                    testRecipeRevision
                );
            }
        }
    }

    catch (error)
    {
        testFailed = !recipeTestAbort || Boolean(session.error);
        finalStatus =
            testFailed ? '테스트 오류: ' + error.message : '테스트 정지';

        if (testFailed) throw error;
    }

    finally
    {
        const interrupted = recipeTestAbort;
        try
        {
            if (session.token && !session.stopped) await stopRecipePointTest();
            else if (recipeTestStopPromise) await recipeTestStopPromise;
            if (session.error) throw session.error;
        }
        catch (error)
        {
            finalStatus = '테스트 정지 오류: ' + error.message;
            throw error;
        }
        finally
        {
            if (interrupted && !testFailed && !finalStatus.startsWith('테스트 정지 오류:'))
            {
                finalStatus = '테스트 정지';
            }
            recipeTestRunning = false;
            recipeTestAbort = false;
            clearRecipeTestTimers(session);
            session.controller.abort();
            if (recipeTestSession === session) recipeTestSession = null;
            updateRecipeTestButtons();
            setRecipeTestStatus(finalStatus);
        }
    }
}


async function stopOperatorOutputs()
{
    const send = async command =>
    {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 2000);
        try { return await request('/api/command', {command, params: {}}, controller.signal); }
        finally { clearTimeout(timeout); }
    };
    let stopped = {};
    let stopError = null;
    try { stopped = await send('motion.stop'); }
    catch (error) { stopError = error; }
    const off = await send('laser.off');
    if (off.laser_on !== false) throw Error('레이저 OFF를 확인하지 못했습니다.');
    if (stopError) throw stopError;
    return {...stopped, laser_on: false};
}


async function stopRecipePointTest({operator = false} = {})
{
    recipeTestAbort =
        true;


    if (recipeTestRunning)
    {
        setRecipeTestStatus(
            '정지 요청 중'
        );
    }


    const pending = recipeTestCommandPromise;
    if (!recipeTestStopPromise) recipeTestStopPromise = (async () => {
        const session = recipeTestSession;
        if (session)
        {
            session.closing = true;
            try
            {
                // A start response may still be carrying the token needed to cancel.
                if (!session.token && pending) await pending.catch(() => {});
                if (!session.token) throw session.error || Error('테스트 시작을 확인하지 못했습니다. 장비는 연결 유지 만료 후 정지합니다.');
                clearRecipeTestTimers(session);
                session.stopController = new AbortController();
                const timeout = setTimeout(() => session.stopController.abort(), 2000);
                try
                {
                    const stopped = await cmd('test.stop', {test_token: session.token}, true, true);
                    if (stopped.accepted !== false && stopped.laser_on !== false)
                    {
                        throw Error('레이저 OFF를 확인하지 못했습니다.');
                    }
                    session.stopped = true;
                    if (stopped.accepted !== false && status.operation)
                    {
                        status.operation = {...status.operation, mode: 'idle', test_active: false, manual_active: false};
                    }
                    return stopped;
                }
                finally { clearTimeout(timeout); }
            }
            finally
            {
                clearRecipeTestTimers(session);
                session.controller.abort();
            }
        }
        // Stop immediately, then repeat after an already submitted move/pulse.
        // A late HTTP response must not leave motion or illumination restarted.
        let stopped = {};
        let stopError = null;
        try { stopped = await cmd('motion.stop', {}, true, true); }
        catch (error) { stopError = error; }
        if (pending)
        {
            await pending.catch(() => {});
            try
            {
                stopped = await cmd('motion.stop', {}, true, true);
                stopError = null;
            }
            catch (error) { stopError = error; }
        }
        const off = await cmd('laser.off', {}, true, true);
        if (off.laser_on !== false) throw Error('레이저 OFF를 확인하지 못했습니다.');
        if (stopError) throw stopError;
        return {...stopped, laser_on: false};
    })();
    const stopping = recipeTestStopPromise;
    try
    {
        const stopped = await stopping;
        return operator && stopped.accepted === false ? await stopOperatorOutputs() : stopped;
    }
    finally { if (recipeTestStopPromise === stopping) recipeTestStopPromise = null; }
}


function clearPoint()
{
    editId =
        null;


    $('pointTitle').textContent =
        '포인트 추가';


    $('pointForm').reset();
}


function editPoint(point)
{
    editId =
        point.id;


    $('pointTitle').textContent =
        '포인트 수정: '
        + point.name;


    for (
        const element
        of $('pointForm').elements
    )
    {
        if (!element.name)
        {
            continue;
        }


        if (
            element.type ===
            'checkbox'
        )
        {
            element.checked =
                point[element.name];
        }

        else
        {
            element.value =
                point[element.name]
                ?? '';
        }
    }
}


function formatRecipeNumber(value)
{
    if ((typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') return '—';
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(3) : '—';
}

function formatRecipeNote(note)
{
    const text = String(note || '');
    if (!text.startsWith('CAD_TARGET ')) return text;
    const lines = text.replace(/^(CAD_TARGET [^\r\n]*?)(도면 )/, '$1\n$2').split(/\r?\n/);
    const coordinates = lines[1]?.match(/^도면 .* \/ X=([+-]?\d+(?:\.\d+)?), Y=([+-]?\d+(?:\.\d+)?), Z=([+-]?\d+(?:\.\d+)?) \/ contact-Z (?:인증|미인증)$/);
    if (!coordinates) return text;
    return [`X ${formatRecipeNumber(coordinates[1])} · Y ${formatRecipeNumber(coordinates[2])} · Z ${formatRecipeNumber(coordinates[3])}`,
        ...lines.slice(2)].join('\n');
}

function estimatePointMoveSeconds(previous, point)
{
    if (!previous || ![previous.pan, previous.tilt, point.pan, point.tilt].every(Number.isFinite)) return null;
    const pan = Math.abs(point.pan - previous.pan) % 360;
    return Math.max(Math.min(pan, 360 - pan) / 30, Math.abs(point.tilt - previous.tilt) / 15);
}

function renderPoints()
{
    const recipe =
        selected();


    requestRecipeEstimates();


    $('recipeDescription').textContent = recipe
        ? `ID ${recipe.index} · 제품 회전 ${recipe.product_rotation_deg || 0}° / 틸팅 ${recipe.product_tilt_deg || 0}°${recipe.description ? ` · ${recipe.description}` : ''}`
        : '';


    selectedPoint();
    updateSelectedPointStatus();
    updateRecipeTestButtons();


    $('points').replaceChildren(

        ...(recipe?.points || [])
            .map((point, index, points) => {

                const tr =
                    document.createElement(
                        'tr'
                    );


                tr.onclick =
                    () => {

                        selectedPointId =
                            point.id;

                        renderPoints();
                    };


                tr.setAttribute(
                    'aria-selected',

                    String(
                        point.id ===
                        selectedPointId
                    )
                );


                tr.classList.toggle(
                    'selected-point',
                    point.id ===
                    selectedPointId
                );


                const estimate = recipeEstimates.get(point.id);
                const moveSeconds = estimatePointMoveSeconds(points[index - 1], point);
                const values = [

                    point.order,

                    point.name,

                    point.pan,

                    point.tilt,

                    formatRecipeNumber(estimate?.x),
                    formatRecipeNumber(estimate?.y),
                    formatRecipeNumber(estimate?.z),
                    moveSeconds === null ? '—' : moveSeconds.toFixed(2),

                    point.enabled
                        ? 'ON'
                        : 'OFF',

                    formatRecipeNote(point.note)
                ];


                for (
                    const [valueIndex, value]
                    of values.entries()
                )
                {
                    const td =
                        document.createElement(
                            'td'
                        );


                    td.textContent =
                        value;


                    if (valueIndex >= 4 && valueIndex <= 6)
                    {
                        td.className = 'estimated-coordinate';
                        td.title = !estimate ? recipeEstimateStatus
                            : valueIndex === 6 && estimate.z == null ? 'Z 추정값 미확정'
                            : estimate.units || '';
                    }
                    if (valueIndex === 7) td.className = 'estimated-move-time';
                    if (valueIndex === 9) td.className = 'recipe-note';


                    tr.append(td);
                }


                const td =
                    document.createElement(
                        'td'
                    );


                td.append(

                    button(
                        '이동',

                        () => cmd(
                            'point.goto',

                            {
                                recipe_id:
                                    recipe.id,

                                point_id:
                                    point.id
                            }
                        )
                    ),


                    button(
                        '편집',

                        () =>
                            editPoint(point)
                    ),


                    button(
                        '↑',

                        () =>
                            reorder(
                                point.id,
                                -1
                            ),

                        '위로'
                    ),


                    button(
                        '↓',

                        () =>
                            reorder(
                                point.id,
                                1
                            ),

                        '아래로'
                    ),


                    button(
                        '삭제',

                        async () => {

                            if (
                                confirm(
                                    point.name
                                    + ' 삭제?'
                                )
                            )
                            {
                                await cmd(
                                    'point.delete',

                                    {
                                        recipe_id:
                                            recipe.id,

                                        point_id:
                                            point.id
                                    }
                                );


                                clearPoint();

                                await loadRecipes();
                            }
                        }
                    )
                );


                tr.append(td);


                return tr;
            })
    );


    window.dispatchEvent(
        new CustomEvent(
            'recipe-point-selected',
            {detail: {point_id: selectedPointId}}
        )
    );
}


async function reorder(
    id,
    delta
)
{
    const recipe =
        selected();


    const ids =
        recipe.points.map(
            point =>
                point.id
        );


    const index =
        ids.indexOf(id);


    const target =
        index + delta;


    if (
        target < 0 ||
        target >= ids.length
    )
    {
        return;
    }


    [
        ids[index],
        ids[target]
    ] =
    [
        ids[target],
        ids[index]
    ];


    await cmd(
        'point.reorder',

        {
            recipe_id:
                recipe.id,

            ordered_ids:
                ids
        }
    );


    await loadRecipes();
}


async function exportRecipeFile()
{
    const recipe = selected();
    if (!recipe)
    {
        throw Error('내보낼 레시피가 없습니다.');
    }
    const result = await cmd(
        'recipe.export',
        {recipe_id: recipe.id},
        true
    );
    const url = URL.createObjectURL(
        new Blob(
            [JSON.stringify(result.document, null, 2)],
            {type: 'application/json'}
        )
    );
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download =
        `${recipe.name.replace(/[^0-9A-Za-z가-힣._-]+/g, '_') || 'recipe'}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}


async function importRecipeFile()
{
    const file = $('recipeFile').files?.[0];
    if (!file)
    {
        return;
    }
    let documentPayload;
    try
    {
        documentPayload = JSON.parse(await file.text());
    }
    catch (error)
    {
        throw Error(`레시피 JSON 오류: ${error.message}`);
    }
    const result = await cmd(
        'recipe.import',
        {document: documentPayload},
        true
    );
    await loadRecipes(result.recipes[0]?.id);
    $('notice').textContent = `레시피 ${result.recipes.length}개를 불러왔습니다.`;
}


$('exportRecipe').onclick =
    guarded(exportRecipeFile);


$('importRecipe').onclick =
    () => $('recipeFile').click();


$('recipeFile').onchange =
    guarded(
        async () => {
            try
            {
                await importRecipeFile();
            }
            finally
            {
                $('recipeFile').value = '';
            }
        }
    );


$('recipeSelect').onchange =
    () => {

        clearPoint();

        selectedPointId =
            null;

        renderPoints();
    };


window.addEventListener(
    'cad-point-selected',
    event => {
        const targetId = String(event.detail?.target_id || '');
        const matches = [];
        for (const recipe of recipes)
        {
            for (const point of recipe.points || [])
            {
                if (point.id === targetId)
                {
                    matches.push({recipe, point});
                }
            }
        }
        if (matches.length !== 1)
        {
            return;
        }
        $('recipeSelect').value = matches[0].recipe.id;
        selectedPointId = matches[0].point.id;
        renderPoints();
        $('selectedPointStatus').textContent +=
            ` · CAD (${event.detail.group_id}, ${targetId}) ID 일치`;
    }
);


let managedRecipeId = null;

function nextRecipeIndex()
{
    const used = new Set(recipes.map(recipe => recipe.index));
    let index = 1;
    while (used.has(index))
    {
        index += 1;
    }
    return index;
}

function renderRecipeManager(recipeId = managedRecipeId)
{
    const list = $('recipeManagerSelect');
    list.replaceChildren(
        ...recipes.map(recipe =>
            new Option(`ID ${recipe.index} · ${recipe.name}`, recipe.id)
        )
    );
    if (recipeId && recipes.some(recipe => recipe.id === recipeId))
    {
        list.value = recipeId;
    }
    else
    {
        list.selectedIndex = -1;
    }
    managedRecipeId = list.value || null;
    const recipe = recipes.find(item => item.id === managedRecipeId);
    $('recipeManagerIndex').value = recipe?.index ?? nextRecipeIndex();
    $('recipeManagerName').value = recipe?.name || '';
    $('recipeManagerDescription').value = recipe?.description || '';
    $('recipeProductRotation').value = recipe?.product_rotation_deg ?? 0;
    $('recipeProductTilt').value = recipe?.product_tilt_deg ?? 0;
    $('deleteManagedRecipe').disabled = !recipe || recipes.length <= 1;
}

$('manageRecipes').onclick = () => {
    renderRecipeManager(selected()?.id || null);
    $('recipeManagerDialog').showModal();
};

$('closeRecipeManager').onclick = () =>
    $('recipeManagerDialog').close();

$('recipeManagerSelect').onchange = event =>
    renderRecipeManager(event.target.value);

$('newManagedRecipe').onclick = () => {
    renderRecipeManager(null);
    $('recipeManagerName').focus();
};

$('recipeManagerForm').onsubmit = guarded(async event => {
    event.preventDefault();
    const result = await cmd('recipe.upsert', {
        ...(managedRecipeId ? {recipe_id: managedRecipeId} : {}),
        index: Number($('recipeManagerIndex').value),
        name: $('recipeManagerName').value.trim(),
        description: $('recipeManagerDescription').value.trim()
        // 제품 회전/틸팅 각도는 레시피에서 더 이상 변경하지 않는다.
        // 실제 제품 자세는 도면 캘리브레이션에서 관리한다.
    });
    clearPoint();
    await loadRecipes(result.recipe.id);
    renderRecipeManager(result.recipe.id);
});

$('deleteManagedRecipe').onclick = guarded(async () => {
    const recipe = recipes.find(item => item.id === managedRecipeId);
    if (!recipe || !confirm(`ID ${recipe.index} · ${recipe.name} 레시피와 포인트를 삭제할까요?`))
    {
        return;
    }
    await cmd('recipe.delete', {recipe_id: recipe.id});
    clearPoint();
    await loadRecipes();
    renderRecipeManager(selected()?.id || null);
});

$('runRecipeSequence').onclick =
    guarded(
        runRecipeSequenceTest
    );


$('runRecipeRandom').onclick =
    guarded(
        runRecipeRandomTest
    );


$('stopRecipeTest').onclick =
    guarded(
        () => stopRecipePointTest({operator: true})
    );


$('pointForm').onsubmit =
    guarded(
        async event => {

            event.preventDefault();


            if (!selected())
            {
                return;
            }


            await cmd(
                'point.upsert',

                {
                    ...formData(
                        event.target
                    ),

                    dwell_ms:
                        selected().points.find(point => point.id === editId)?.dwell_ms
                        ?? 0,

                    recipe_id:
                        selected().id,

                    ...(
                        editId
                            ? {
                                point_id:
                                    editId
                            }
                            : {}
                    )
                }
            );


            clearPoint();


            await loadRecipes();
        }
    );


$('capture').onclick =
    guarded(
        async () => {

            const position =
                await cmd(
                    'position.get'
                );


            if (
                position.pan === null ||
                position.tilt === null
            )
            {
                throw Error(
                    '위치 응답 없음'
                );
            }


            $('pointForm')
                .elements
                .pan
                .value =
                position.pan;


            $('pointForm')
                .elements
                .tilt
                .value =
                position.tilt;
        }
    );


$('newPoint').onclick =
    clearPoint;


// ============================================================================
// Drawing Calibration
// ============================================================================

function setDrawingStatus(text)
{
    if ($('drawingStatus'))
    {
        $('drawingStatus').textContent =
            text;
    }
}


function setDrawingProgress(percent, stage, detail = '')
{
    const panel =
        $('drawingProgressPanel');

    if (!panel)
    {
        return;
    }


    panel.hidden =
        false;

    const value =
        Math.max(
            0,
            Math.min(
                100,
                Math.round(percent)
            )
        );

    $('drawingProgressBar').value =
        value;

    $('drawingProgressPercent').textContent =
        `${value}%`;

    $('drawingProgressStage').textContent =
        stage;

    $('drawingProgressDetail').textContent =
        detail;

    setDrawingStatus(
        `${stage} ${value}%`
    );
}


function finishDrawingProgress(stage, detail)
{
    if (drawingProgressTimer)
    {
        clearInterval(
            drawingProgressTimer
        );

        drawingProgressTimer =
            null;
    }

    setDrawingProgress(
        100,
        stage,
        detail
    );
}


function failDrawingProgress(message)
{
    if (drawingProgressTimer)
    {
        clearInterval(
            drawingProgressTimer
        );

        drawingProgressTimer =
            null;
    }

    setDrawingStatus(
        '변환 실패: ' + message
    );

    if ($('drawingProgressPanel'))
    {
        $('drawingProgressPanel').hidden =
            false;

        $('drawingProgressStage').textContent =
            '변환 실패';

        $('drawingProgressDetail').textContent =
            message;
    }
}


function startServerConversionProgress(file)
{
    if (drawingProgressTimer)
    {
        clearInterval(
            drawingProgressTimer
        );
    }


    let percent =
        46;

    const steps = [
        [58, '파일 형식 판별', `${file.name} 분석 중`],
        [72, '3D Mesh 변환', '표시용 삼각면을 생성하는 중'],
        [86, '렌더링 최적화', '브라우저에서 부드럽게 볼 수 있도록 정리하는 중'],
        [94, '서버 저장', '최근 항목에 저장하는 중']
    ];

    let index =
        0;

    drawingProgressTimer =
        setInterval(
            () => {

                const step =
                    steps[Math.min(index, steps.length - 1)];

                percent =
                    Math.min(
                        step[0],
                        percent + Math.max(1, Math.round((step[0] - percent) * 0.22))
                    );

                setDrawingProgress(
                    percent,
                    step[1],
                    step[2]
                );

                if (
                    percent >= step[0] - 1 &&
                    index < steps.length - 1
                )
                {
                    index += 1;
                }
            },
            650
        );
}


function uploadDrawingRequest(file, data, files = [file])
{
    return new Promise(
        (resolve, reject) => {

            const xhr =
                new XMLHttpRequest();

            let conversionStarted =
                false;

            const totalSize =
                files.reduce(
                    (sum, item) =>
                        sum + item.size,
                    0
                ) ||
                file.size;

            const fileSummary =
                files.length > 1
                    ? `${file.name} + ${files.length - 1} files`
                    : file.name;

            xhr.open(
                'POST',
                '/api/drawings/upload'
            );

            xhr.timeout =
                600000;

            xhr.upload.onprogress =
                event => {

                    if (
                        event.lengthComputable
                    )
                    {
                        const uploadPercent =
                            Math.min(
                                45,
                                Math.max(
                                    3,
                                    event.loaded / event.total * 45
                                )
                            );

                        setDrawingProgress(
                            uploadPercent,
                            '파일 업로드',
                            `${(event.loaded / 1024 / 1024).toFixed(1)} / ${(event.total / 1024 / 1024).toFixed(1)} MB`
                        );
                    }
                    else
                    {
                        setDrawingProgress(
                            12,
                            '파일 업로드',
                            fileSummary
                        );
                    }
                };

            xhr.upload.onload =
                () => {

                    conversionStarted =
                        true;

                    startServerConversionProgress(
                        file
                    );
                };

            xhr.onload =
                () => {

                    if (!conversionStarted)
                    {
                        startServerConversionProgress(
                            file
                        );
                    }

                    let json;

                    try
                    {
                        json =
                            JSON.parse(
                                xhr.responseText || '{}'
                            );
                    }
                    catch
                    {
                        reject(
                            Error(
                                xhr.responseText ||
                                xhr.statusText ||
                                '서버 응답을 해석할 수 없습니다.'
                            )
                        );

                        return;
                    }

                    if (
                        xhr.status < 200 ||
                        xhr.status >= 300 ||
                        json.ok === false
                    )
                    {
                        reject(
                            Error(
                                json.error?.message ||
                                xhr.statusText ||
                                '알 수 없는 오류'
                            )
                        );

                        return;
                    }

                    resolve(
                        json
                    );
                };

            xhr.onerror =
                () =>
                    reject(
                        Error('업로드 연결에 실패했습니다.')
                    );

            xhr.ontimeout =
                () =>
                    reject(
                        Error('도면 변환이 10분을 넘겨 중단했습니다.')
                    );

            setDrawingProgress(
                2,
                '업로드 준비',
                `${fileSummary} (${(totalSize / 1024 / 1024).toFixed(1)} MB)`
            );

            xhr.send(
                data
            );
        }
    );
}


function activeDrawingId()
{
    return activeDrawing?.id || $('drawingSelect')?.value || '';
}


async function loadDrawings(selectId)
{
    if (!$('drawingSelect'))
    {
        return;
    }


    drawings =
        (
            await cmd(
                'drawing.list',
                {},
                true
            )
        ).drawings;


    const id =
        selectId ||
        $('drawingSelect').value ||
        drawings[0]?.id ||
        '';


    $('drawingSelect').replaceChildren(

        ...drawings.map(
            drawing =>
                new Option(
                    `${drawing.name} (${drawing.product_id || 'CAD'})`,
                    drawing.id
                )
        )
    );


    if (
        drawings.some(
            drawing =>
                drawing.id === id
        )
    )
    {
        $('drawingSelect').value =
            id;
    }
}


async function uploadDrawingFile()
{
    const files =
        Array.from(
            $('drawingFile').files || []
        );


    if (!files.length)
    {
        throw Error(
            '업로드할 도면 파일을 선택하세요.'
        );
    }

    try
    {
        await ensureDrawingWorkflow();
    }
    catch (error)
    {
        failDrawingProgress(error.message);
        throw error;
    }


    setDrawingStatus(
        '변환 중...'
    );


    const primaryFile =
        files.find(
            file =>
                /\.(stp|step|cad)$/i.test(
                    file.name
                )
        ) ||
        files[0];


    const data =
        new FormData();

    for (const file of files)
    {
        data.append(
            'file',
            file
        );
    }


    let json;

    try
    {
        json =
            await uploadDrawingRequest(
                primaryFile,
                data,
                files
            );
    }
    catch (error)
    {
        failDrawingProgress(
            error.message
        );

        throw Error(
            error.message
        );
    }


    const drawing =
        json.result.drawing;

    await loadRecipes();

    setDrawingProgress(
        96,
        '목록 갱신',
        '최근 도면 목록을 갱신하는 중'
    );

    await loadDrawings(
        drawing.id
    );

    setDrawingProgress(
        98,
        '모델 불러오기',
        '변환된 mesh를 뷰어에 올리는 중'
    );

    await loadDrawing(
        drawing.id
    );

    finishDrawingProgress(
        '완료',
        `${drawing.name} 변환 완료`
    );

    renderDrawingManager();
}


function renderDrawingManager()
{
    const root = $('drawingManagerList');
    if (!root)
    {
        return;
    }
    root.replaceChildren(...drawings.map((drawing, index) => {
        const row = document.createElement('article');
        row.className = 'drawing-manager-item';
        const info = document.createElement('div');
        const title = document.createElement('input');
        title.value = drawing.name;
        title.maxLength = 200;
        title.setAttribute('aria-label', `${index + 1}번 도면 이름`);
        const rename = document.createElement('button');
        rename.type = 'button';
        rename.textContent = '이름 저장';
        rename.onclick = drawingAction(async () => {
            const result = await cmd('drawing.update', {drawing_id: drawing.id, name: title.value}, true);
            if (activeDrawing?.id === drawing.id)
            {
                activeDrawing = result.drawing;
                renderDrawingPanel();
            }
            await loadDrawings();
            renderDrawingManager();
        }, {pending: '도면 이름 저장 중…', done: '도면 이름을 저장했습니다.', statusId: 'drawingManagerStatus'});
        const files = document.createElement('p');
        files.textContent = [drawing.source_filename, ...(drawing.attachment_filenames || [])]
            .filter(Boolean)
            .join(' · ');
        info.append(title, files);
        const action = document.createElement('button');
        action.type = 'button';
        action.textContent = '삭제';
        action.onclick = drawingAction(async () => {
            if (!confirm(`${drawing.name} 도면과 포인트 파일을 삭제할까요?`))
            {
                return false;
            }
            await cmd('drawing.delete', {drawing_id: drawing.id}, true);
            if (activeDrawing?.id === drawing.id)
            {
                activeDrawing = null;
                selectedDrawingPointId = null;
                renderDrawingPanel();
            }
            await loadDrawings();
            renderDrawingManager();
        }, {pending: '도면 삭제 중…', done: '도면을 삭제했습니다.', statusId: 'drawingManagerStatus'});
        const download = document.createElement('button');
        download.type = 'button';
        download.textContent = 'ZIP 내보내기';
        download.onclick = drawingAction(async () => {
            const response = await fetch(`/api/drawings/${encodeURIComponent(drawing.id)}/archive`);
            if (!response.ok) throw Error('도면 ZIP 내보내기에 실패했습니다.');
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url;
            link.download = `${drawing.name.replace(/[\\/:*?"<>|]/g, '_')}_PTM.zip`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }, {pending: '도면 ZIP 내보내는 중…', done: '도면 ZIP 다운로드를 시작했습니다.', statusId: 'drawingManagerStatus'});
        const actions = document.createElement('div');
        actions.className = 'toolbar';
        for (const [label, delta] of [['위로', -1], ['아래로', 1]])
        {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = label;
            button.disabled = index + delta < 0 || index + delta >= drawings.length;
            button.onclick = drawingAction(async () => {
                const ids = drawings.map(item => item.id);
                [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
                await cmd('drawing.reorder', {ordered_ids: ids}, true);
                await loadDrawings();
                renderDrawingManager();
            }, {pending: '도면 순서 저장 중…', done: '도면 순서를 저장했습니다.', statusId: 'drawingManagerStatus'});
            actions.append(button);
        }
        actions.append(rename, download, action);
        row.append(info, actions);
        return row;
    }));
}


async function openDrawingManager()
{
    await loadDrawings();
    renderDrawingManager();
    $('drawingManagerDialog').showModal();
}


async function loadDrawing(id = $('drawingSelect').value || activeDrawing?.id)
{
    if (!id)
    {
        throw Error('불러올 도면을 선택하세요.');
    }


    setDrawingStatus(
        '불러오는 중...'
    );


    activeDrawing =
        (
            await cmd(
                'drawing.get',
                {
                    drawing_id:
                        id
                },
                true
            )
        ).drawing;


    selectedDrawingPointId =
        activeDrawing.points?.[0]?.id ||
        null;

    loadDrawingPoseInputs();
    renderDrawingPanel();
    const detail = {drawing: activeDrawing};
    window.dispatchEvent(new CustomEvent('drawing-cad-load', {detail}));
    if (detail.ready) await detail.ready;

    setDrawingStatus(
        '도면 로드 완료'
    );
}


function resetDrawingView()
{
    applyDrawingViewPreset(
        'iso',
        false
    );

    drawingView.zoom =
        1;

    drawingView.panX =
        0;

    drawingView.panY =
        0;

    drawingView.interacting =
        false;

    if ($('drawingViewPreset'))
    {
        $('drawingViewPreset').value =
            'iso';
    }

    scheduleDrawingCanvas();
}


function applyDrawingViewPreset(preset, redraw = true)
{
    const presets = {
        iso: [-35, 22],
        front: [0, 0],
        top: [0, 89],
        bottom: [180, -89],
        right: [-90, 0],
        back: [180, 0]
    };

    const next =
        presets[preset] ||
        presets.iso;

    drawingView.viewPreset =
        preset in presets
            ? preset
            : 'iso';

    drawingView.yaw =
        next[0];

    drawingView.pitch =
        next[1];

    drawingView.zoom =
        1;

    drawingView.panX =
        0;

    drawingView.panY =
        0;

    if (redraw)
    {
        scheduleDrawingCanvas();
    }
}


function scheduleDrawingCanvas()
{
    if (drawingRedrawFrame)
    {
        return;
    }

    const requestFrame =
        window.requestAnimationFrame ||
        (
            callback =>
                setTimeout(
                    callback,
                    16
                )
        );

    drawingRedrawFrame =
        requestFrame(
            () => {

                drawingRedrawFrame =
                    null;

                drawDrawingCanvas();
            }
        );
}


function drawingFaceLimit()
{
    const limits = {
        fast: 7000,
        balanced: 18000,
        high: 42000
    };

    const base =
        limits[drawingView.quality] ||
        limits.balanced;

    return drawingView.interacting
        ? Math.min(base, 6000)
        : base;
}


function drawingSampleStep(count, limit)
{
    return Math.max(
        1,
        Math.ceil(count / Math.max(1, limit))
    );
}


function triangleArea(points)
{
    return Math.abs(
        (
            points[0].x * (points[1].y - points[2].y) +
            points[1].x * (points[2].y - points[0].y) +
            points[2].x * (points[0].y - points[1].y)
        ) * 0.5
    );
}


function isStepPreviewMesh()
{
    if (
        typeof drawingMesh?.step_preview === 'boolean'
    )
    {
        return drawingMesh.step_preview;
    }

    const source =
        String(
            drawingMesh?.source ||
            activeDrawing?.source_filename ||
            ''
        ).toLowerCase();

    return (
        source.endsWith('.stp') ||
        source.endsWith('.step') ||
        source.endsWith('.cad')
    );
}


function shouldSkipCleanFace(points)
{
    const area =
        triangleArea(points);

    if (area < 0.8)
    {
        return true;
    }

    const a =
        Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);

    const b =
        Math.hypot(points[1].x - points[2].x, points[1].y - points[2].y);

    const c =
        Math.hypot(points[2].x - points[0].x, points[2].y - points[0].y);

    const longest =
        Math.max(a, b, c);

    const canvas =
        $('drawingCanvas');

    const canvasArea =
        Math.max(
            1,
            (canvas?.width || 1) *
            (canvas?.height || 1)
        );

    const viewport =
        Math.min(
            canvas?.width || 1,
            canvas?.height || 1
        );

    if (
        isStepPreviewMesh() &&
        (
            area > canvasArea * 0.035 ||
            longest > viewport * 0.68
        )
    )
    {
        return true;
    }

    return longest > 140 && area / Math.max(1, longest * longest) < 0.003;
}


function drawStepPreviewEdges(ctx, items)
{
    const edgeMap =
        new Map();

    for (const item of items)
    {
        const pairs = [
            [0, 1],
            [1, 2],
            [2, 0]
        ];

        for (const [from, to] of pairs)
        {
            const vertexA =
                item.face[from];

            const vertexB =
                item.face[to];

            const pointA =
                item.points[from];

            const pointB =
                item.points[to];

            if (
                !pointA ||
                !pointB
            )
            {
                continue;
            }

            const key =
                vertexA < vertexB
                    ? `${vertexA}:${vertexB}`
                    : `${vertexB}:${vertexA}`;

            const length =
                Math.hypot(
                    pointA.x - pointB.x,
                    pointA.y - pointB.y
                );

            if (!Number.isFinite(length))
            {
                continue;
            }

            const edge =
                edgeMap.get(key) ||
                {
                    count: 0,
                    pointA,
                    pointB,
                    length
                };

            edge.count += 1;
            edge.length =
                Math.min(
                    edge.length,
                    length
                );

            edgeMap.set(
                key,
                edge
            );
        }
    }

    const canvas =
        $('drawingCanvas');

    const viewport =
        Math.min(
            canvas?.width || 1,
            canvas?.height || 1
        );

    let edges =
        Array.from(
            edgeMap.values()
        ).filter(
            edge =>
                edge.length < viewport * 0.46 &&
                (
                    edge.count === 1 ||
                    edge.length < viewport * 0.07
                )
        );

    if (edges.length < 24)
    {
        edges =
            Array.from(
                edgeMap.values()
            ).filter(
                edge =>
                    edge.length < viewport * 0.38
            );
    }

    ctx.strokeStyle =
        'rgba(91,117,140,0.72)';

    ctx.lineWidth =
        1.05 * (window.devicePixelRatio || 1);

    ctx.beginPath();

    for (const edge of edges)
    {
        ctx.moveTo(
            edge.pointA.x,
            edge.pointA.y
        );

        ctx.lineTo(
            edge.pointB.x,
            edge.pointB.y
        );
    }

    ctx.stroke();
}


function colorFromFace(item)
{
    const sourceColor =
        drawingMesh.face_colors?.[item.index];

    if (sourceColor)
    {
        return `rgba(${sourceColor[0]},${sourceColor[1]},${sourceColor[2]},0.96)`;
    }

    const palette = [
        [116, 100, 172],
        [142, 145, 166],
        [202, 120, 102],
        [89, 150, 119],
        [196, 150, 84],
        [94, 157, 178],
        [164, 122, 158],
        [135, 139, 112]
    ];

    const bucket =
        Math.abs(
            Math.floor(
                item.center[0] * 13 +
                item.center[1] * 17 +
                item.center[2] * 19
            )
        ) % palette.length;

    const base =
        palette[bucket];

    const light =
        Math.max(
            0.62,
            Math.min(
                1.16,
                0.92 + item.depth * 0.42
            )
        );

    return `rgba(${Math.round(base[0] * light)},${Math.round(base[1] * light)},${Math.round(base[2] * light)},0.97)`;
}


function shadedFaceColor(item)
{
    const color =
        drawingMesh.face_colors?.[item.index];

    if (color)
    {
        return `rgba(${color[0]},${color[1]},${color[2]},0.9)`;
    }

    const shade =
        Math.max(
            58,
            Math.min(
                220,
                122 + item.depth * 92
            )
        );

    return `rgba(${shade},${Math.min(245, shade + 26)},${Math.min(255, shade + 44)},0.94)`;
}


function updateDrawingOrientationCube()
{
    const cube =
        $('drawingOrientationCube');

    if (!cube)
    {
        return;
    }

    cube.style.transform =
        `rotateX(${-drawingView.pitch}deg) rotateY(${drawingView.yaw}deg)`;
}


function drawingCenterScale()
{
    const bounds =
        drawingMesh?.bounds;


    if (!bounds)
    {
        return {
            center: [0, 0, 0],
            scale: 1
        };
    }


    const min =
        bounds.min;

    const max =
        bounds.max;

    return {
        center: [
            (min[0] + max[0]) * 0.5,
            (min[1] + max[1]) * 0.5,
            (min[2] + max[2]) * 0.5
        ],
        scale:
            Math.max(
                max[0] - min[0],
                max[1] - min[1],
                max[2] - min[2],
                1
            )
    };
}


function resizeDrawingCanvas()
{
    const canvas =
        $('drawingCanvas');

    if (!canvas)
    {
        return;
    }


    const rect =
        canvas.getBoundingClientRect();

    const ratio =
        window.devicePixelRatio || 1;

    const width =
        Math.max(
            320,
            Math.round(rect.width * ratio)
        );

    const height =
        Math.max(
            260,
            Math.round(rect.height * ratio)
        );


    if (
        canvas.width !== width ||
        canvas.height !== height
    )
    {
        canvas.width =
            width;

        canvas.height =
            height;
    }
}


function projectDrawingVertex(vertex, viewInfo)
{
    const center =
        viewInfo.center;

    const scale =
        viewInfo.scale;

    const yaw =
        drawingView.yaw * Math.PI / 180;

    const pitch =
        drawingView.pitch * Math.PI / 180;

    const x =
        (vertex[0] - center[0]) / scale;

    const y =
        (vertex[1] - center[1]) / scale;

    const z =
        (vertex[2] - center[2]) / scale;

    const x1 =
        x * Math.cos(yaw) - y * Math.sin(yaw);

    const y1 =
        x * Math.sin(yaw) + y * Math.cos(yaw);

    const y2 =
        y1 * Math.cos(pitch) - z * Math.sin(pitch);

    const z2 =
        y1 * Math.sin(pitch) + z * Math.cos(pitch);

    const distance =
        4;

    const factor =
        distance / Math.max(
            0.25,
            distance - z2
        );

    const canvas =
        $('drawingCanvas');

    const base =
        Math.min(
            canvas.width,
            canvas.height
        ) * 0.62 * drawingView.zoom;

    return {
        x:
            canvas.width * 0.5 +
            drawingView.panX +
            x1 * base * factor,

        y:
            canvas.height * 0.5 +
            drawingView.panY -
            y2 * base * factor,

        depth:
            z2,

        original:
            vertex
    };
}


function renderDrawingCanvasOptimized(ctx, canvas)
{
    const mode =
        drawingView.renderMode ||
        'clean';

    const cleanMode =
        mode === 'clean';

    const stepPreviewMode =
        isStepPreviewMesh();

    ctx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    ctx.fillStyle =
        cleanMode
            ? '#eef0f2'
            : '#0f141b';

    ctx.fillRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    if (!cleanMode)
    {
        ctx.strokeStyle =
            '#1d2632';

        ctx.lineWidth =
            1;

        for (
            let x = 0;
            x < canvas.width;
            x += 48
        )
        {
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, canvas.height);
            ctx.stroke();
        }

        for (
            let y = 0;
            y < canvas.height;
            y += 48
        )
        {
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(canvas.width, y);
            ctx.stroke();
        }
    }


    if (
        !drawingMesh ||
        !drawingMesh.vertices?.length
    )
    {
        ctx.fillStyle =
            cleanMode
                ? '#687487'
                : '#8fa3ba';

        ctx.font =
            '16px Segoe UI';

        ctx.textAlign =
            'center';

        ctx.fillText(
            '도면 파일을 업로드하거나 최근 항목을 불러오세요.',
            canvas.width / 2,
            canvas.height / 2
        );

        updateDrawingOrientationCube();

        return;
    }


    const viewInfo =
        drawingCenterScale();

    drawingProjected =
        drawingMesh.vertices.map(
            vertex =>
                projectDrawingVertex(
                    vertex,
                    viewInfo
                )
        );


    const faces =
        drawingMesh.faces || [];

    const limit =
        drawingFaceLimit();

    const step =
        drawingSampleStep(
            faces.length,
            limit
        );

    const sampledFaces =
        step === 1
            ? faces
            : faces.filter(
                (_face, index) =>
                    index % step === 0
            );

    drawingFacesForPick =
        sampledFaces;

    if (
        faces.length &&
        mode !== 'points'
    )
    {
        const items = [];

        sampledFaces.forEach(
            (face, sampleIndex) => {

                const sourceIndex =
                    step === 1
                        ? sampleIndex
                        : sampleIndex * step;

                const points =
                    face.map(
                        vertexIndex =>
                            drawingProjected[vertexIndex]
                    );

                if (
                    (
                        (
                            cleanMode &&
                            !stepPreviewMode
                        ) ||
                        (
                            !cleanMode &&
                            stepPreviewMode
                        )
                    ) &&
                    shouldSkipCleanFace(points)
                )
                {
                    return;
                }

                items.push(
                    {
                        index:
                            sourceIndex,

                        face,

                        points,

                        center: [
                            (
                                points[0].original[0] +
                                points[1].original[0] +
                                points[2].original[0]
                            ) / 3,
                            (
                                points[0].original[1] +
                                points[1].original[1] +
                                points[2].original[1]
                            ) / 3,
                            (
                                points[0].original[2] +
                                points[1].original[2] +
                                points[2].original[2]
                            ) / 3
                        ],

                        depth:
                            (
                                points[0].depth +
                                points[1].depth +
                                points[2].depth
                            ) / 3
                    }
                );
            }
        );

        items.sort(
            (a, b) =>
                a.depth - b.depth
        );

        if (
            cleanMode &&
            stepPreviewMode
        )
        {
            drawStepPreviewEdges(
                ctx,
                items
            );
        }
        else if (mode === 'wireframe')
        {
            ctx.strokeStyle =
                cleanMode
                    ? 'rgba(94,104,118,0.72)'
                    : 'rgba(132,176,220,0.62)';

            ctx.lineWidth =
                cleanMode
                    ? 1.1
                    : 0.9;

            for (const item of items)
            {
                ctx.beginPath();
                ctx.moveTo(
                    item.points[0].x,
                    item.points[0].y
                );
                ctx.lineTo(
                    item.points[1].x,
                    item.points[1].y
                );
                ctx.lineTo(
                    item.points[2].x,
                    item.points[2].y
                );
                ctx.closePath();
                ctx.stroke();
            }
        }
        else
        {
            for (const item of items)
            {
                ctx.beginPath();
                ctx.moveTo(
                    item.points[0].x,
                    item.points[0].y
                );
                ctx.lineTo(
                    item.points[1].x,
                    item.points[1].y
                );
                ctx.lineTo(
                    item.points[2].x,
                    item.points[2].y
                );
                ctx.closePath();

                ctx.fillStyle =
                    mode === 'xray'
                        ? 'rgba(118,182,218,0.34)'
                        : cleanMode
                            ? colorFromFace(item)
                            : shadedFaceColor(item);

                ctx.fill();

                if (
                    (
                        cleanMode &&
                        drawingView.quality === 'high' &&
                        items.length < 16000
                    ) ||
                    (
                        !cleanMode &&
                        items.length < 12000
                    )
                )
                {
                    ctx.strokeStyle =
                        cleanMode
                            ? 'rgba(52,59,68,0.1)'
                            : 'rgba(132,176,220,0.18)';

                    ctx.lineWidth =
                        cleanMode
                            ? 0.7
                            : 1;

                    ctx.stroke();
                }
            }
        }

        if (cleanMode)
        {
            ctx.fillStyle =
                'rgba(40,48,58,0.52)';

            ctx.font =
                `${Math.round(12 * (window.devicePixelRatio || 1))}px Segoe UI`;

            ctx.textAlign =
                'left';

            ctx.fillText(
                `표시 면 ${items.length.toLocaleString()} / 전체 ${faces.length.toLocaleString()}`,
                18 * (window.devicePixelRatio || 1),
                canvas.height - 18 * (window.devicePixelRatio || 1)
            );
        }
    }
    else
    {
        const pointStep =
            Math.max(
                1,
                Math.ceil(
                    drawingProjected.length /
                    (
                        drawingView.interacting
                            ? 8000
                            : 22000
                    )
                )
            );

        ctx.fillStyle =
            cleanMode
                ? '#5d6f85'
                : '#95b6d6';

        for (
            let index = 0;
            index < drawingProjected.length;
            index += pointStep
        )
        {
            const point =
                drawingProjected[index];

            ctx.fillRect(
                point.x,
                point.y,
                2,
                2
            );
        }
    }

    updateDrawingOrientationCube();
    drawDrawingPoints(ctx);
}


function drawDrawingCanvas()
{
    const canvas =
        $('drawingCanvas');

    if (!canvas)
    {
        return;
    }


    resizeDrawingCanvas();

    const ctx =
        canvas.getContext('2d');

    renderDrawingCanvasOptimized(
        ctx,
        canvas
    );

    return;

    ctx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    ctx.fillStyle =
        '#0f141b';

    ctx.fillRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    ctx.strokeStyle =
        '#1d2632';

    ctx.lineWidth =
        1;

    for (
        let x = 0;
        x < canvas.width;
        x += 48
    )
    {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, canvas.height);
        ctx.stroke();
    }

    for (
        let y = 0;
        y < canvas.height;
        y += 48
    )
    {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(canvas.width, y);
        ctx.stroke();
    }


    if (
        !drawingMesh ||
        !drawingMesh.vertices?.length
    )
    {
        ctx.fillStyle =
            '#8fa3ba';

        ctx.font =
            '16px Segoe UI';

        ctx.textAlign =
            'center';

        ctx.fillText(
            '도면 파일을 업로드하거나 최근 항목을 불러오세요.',
            canvas.width / 2,
            canvas.height / 2
        );

        return;
    }


    const viewInfo =
        drawingCenterScale();

    drawingProjected =
        drawingMesh.vertices.map(
            vertex =>
                projectDrawingVertex(
                    vertex,
                    viewInfo
                )
        );


    const faces =
        drawingMesh.faces || [];

    drawingFacesForPick =
        faces;

    if (faces.length)
    {
        const items =
            faces.map(
                (face, index) => {

                    const points =
                        face.map(
                            vertexIndex =>
                                drawingProjected[vertexIndex]
                        );

                    return {
                        index,
                        face,
                        points,
                        depth:
                            (
                                points[0].depth +
                                points[1].depth +
                                points[2].depth
                            ) / 3
                    };
                }
            );


        items.sort(
            (a, b) =>
                a.depth - b.depth
        );


        for (const item of items)
        {
            const color =
                drawingMesh.face_colors?.[item.index];

            const shade =
                Math.max(
                    80,
                    Math.min(
                        220,
                        138 + item.depth * 55
                    )
                );

            ctx.beginPath();
            ctx.moveTo(
                item.points[0].x,
                item.points[0].y
            );
            ctx.lineTo(
                item.points[1].x,
                item.points[1].y
            );
            ctx.lineTo(
                item.points[2].x,
                item.points[2].y
            );
            ctx.closePath();

            ctx.fillStyle =
                color
                    ? `rgba(${color[0]},${color[1]},${color[2]},0.92)`
                    : `rgba(${shade},${Math.min(245, shade + 26)},${Math.min(255, shade + 44)},0.94)`;

            ctx.fill();

            if (
                items.length < 12000
            )
            {
                ctx.strokeStyle =
                    'rgba(132,176,220,0.18)';

                ctx.stroke();
            }
        }
    }
    else
    {
        ctx.fillStyle =
            '#95b6d6';

        for (
            let index = 0;
            index < drawingProjected.length;
            index += Math.max(
                1,
                Math.ceil(drawingProjected.length / 15000)
            )
        )
        {
            const point =
                drawingProjected[index];

            ctx.fillRect(
                point.x,
                point.y,
                2,
                2
            );
        }
    }


    drawDrawingPoints(ctx);
}


function drawDrawingPoints(ctx)
{
    if (!activeDrawing?.points)
    {
        return;
    }


    ctx.font =
        'bold 13px Segoe UI';

    ctx.textAlign =
        'left';

    for (const point of activeDrawing.points)
    {
        const projected =
            projectDrawingVertex(
                [
                    Number(point.x),
                    Number(point.y),
                    Number(point.z)
                ],
                drawingCenterScale()
            );

        const selected =
            point.id === selectedDrawingPointId;

        ctx.beginPath();
        ctx.arc(
            projected.x,
            projected.y,
            selected ? 8 : 6,
            0,
            Math.PI * 2
        );

        ctx.fillStyle =
            point.calibration
                ? '#ffd166'
                : point.pan === null || point.tilt === null
                    ? '#f2f2f2'
                    : '#4dd4ac';

        ctx.fill();

        ctx.lineWidth =
            selected ? 3 : 2;

        ctx.strokeStyle =
            selected ? '#ffffff' : '#151b22';

        ctx.stroke();

        ctx.fillStyle =
            '#eaf4ff';

        ctx.fillText(
            String(point.order),
            projected.x + 9,
            projected.y - 8
        );
    }
}


function canvasPoint(event)
{
    const canvas =
        $('drawingCanvas');

    const rect =
        canvas.getBoundingClientRect();

    return {
        x:
            (event.clientX - rect.left) *
            canvas.width /
            rect.width,

        y:
            (event.clientY - rect.top) *
            canvas.height /
            rect.height
    };
}


function barycentric(point, a, b, c)
{
    const denominator =
        (b.y - c.y) * (a.x - c.x) +
        (c.x - b.x) * (a.y - c.y);

    if (
        Math.abs(denominator) < 1e-9
    )
    {
        return null;
    }


    const w1 =
        (
            (b.y - c.y) * (point.x - c.x) +
            (c.x - b.x) * (point.y - c.y)
        ) / denominator;

    const w2 =
        (
            (c.y - a.y) * (point.x - c.x) +
            (a.x - c.x) * (point.y - c.y)
        ) / denominator;

    const w3 =
        1 - w1 - w2;


    if (
        w1 >= -0.015 &&
        w2 >= -0.015 &&
        w3 >= -0.015
    )
    {
        return [
            w1,
            w2,
            w3
        ];
    }


    return null;
}


function pickDrawingPosition(point)
{
    if (
        !drawingMesh ||
        !drawingProjected.length
    )
    {
        return null;
    }


    let bestDepth =
        -Infinity;

    let best =
        null;


    for (const face of drawingFacesForPick)
    {
        const items =
            face.map(
                index =>
                    drawingProjected[index]
            );

        const weights =
            barycentric(
                point,
                items[0],
                items[1],
                items[2]
            );

        if (!weights)
        {
            continue;
        }


        const depth =
            weights[0] * items[0].depth +
            weights[1] * items[1].depth +
            weights[2] * items[2].depth;


        if (
            depth > bestDepth
        )
        {
            bestDepth =
                depth;

            best = [
                weights[0] * items[0].original[0] +
                    weights[1] * items[1].original[0] +
                    weights[2] * items[2].original[0],
                weights[0] * items[0].original[1] +
                    weights[1] * items[1].original[1] +
                    weights[2] * items[2].original[1],
                weights[0] * items[0].original[2] +
                    weights[1] * items[1].original[2] +
                    weights[2] * items[2].original[2]
            ];
        }
    }


    if (best)
    {
        return best;
    }


    let bestDistance =
        28;

    for (const projected of drawingProjected)
    {
        const distance =
            Math.hypot(
                projected.x - point.x,
                projected.y - point.y
            );

        if (
            distance < bestDistance
        )
        {
            bestDistance =
                distance;

            best =
                projected.original;
        }
    }


    return best;
}


async function addDrawingPoint(position)
{
    if (!activeDrawing)
    {
        throw Error(
            '먼저 도면을 불러오세요.'
        );
    }


    const result =
        await cmd(
            'drawing.point_upsert',

            {
                drawing_id:
                    activeDrawing.id,

                label:
                    `P${(activeDrawing.points?.length || 0) + 1}`,

                x:
                    position[0],

                y:
                    position[1],

                z:
                    position[2]
            },

            true
        );


    activeDrawing =
        result.drawing;

    selectedDrawingPointId =
        result.point.id;

    renderDrawingPanel();
}


function nullableNumber(value)
{
    if (
        value === '' ||
        value === null ||
        value === undefined
    )
    {
        return null;
    }

    return Number(value);
}


function formatDrawingNumber(value, digits = 3)
{
    if (
        value === null ||
        value === undefined ||
        value === ''
    )
    {
        return '-';
    }

    const number =
        Number(value);

    if (!Number.isFinite(number))
    {
        return '-';
    }

    return number
        .toFixed(digits)
        .replace(/\.?0+$/, '');
}


function drawingNumberSpan(value, digits = 3)
{
    const span =
        document.createElement('span');

    span.className =
        'drawing-number';

    span.textContent =
        formatDrawingNumber(
            value,
            digits
        );

    return span;
}


async function reorderDrawingPoints(orderedIds)
{
    if (!activeDrawing)
    {
        return;
    }

    const result =
        await cmd(
            'drawing.point_reorder',

            {
                drawing_id:
                    activeDrawing.id,

                ordered_ids:
                    orderedIds
            },

            true
        );

    activeDrawing =
        result.drawing;

    renderDrawingPanel();
}


async function dropDrawingPoint(targetId)
{
    if (
        !drawingDragPointId ||
        !targetId ||
        drawingDragPointId === targetId ||
        !activeDrawing?.points
    )
    {
        drawingDragPointId =
            null;

        return;
    }

    const ids =
        activeDrawing.points.map(
            point =>
                point.id
        );

    const from =
        ids.indexOf(drawingDragPointId);

    const to =
        ids.indexOf(targetId);

    if (
        from < 0 ||
        to < 0
    )
    {
        drawingDragPointId =
            null;

        return;
    }

    const [moved] =
        ids.splice(
            from,
            1
        );

    ids.splice(
        to,
        0,
        moved
    );

    drawingDragPointId =
        null;

    await reorderDrawingPoints(
        ids
    );
}


function loadDrawingPoseInputs()
{
    if (!$('drawingProductRotation') || !$('drawingProductTilt'))
    {
        return;
    }

    const calibration = activeDrawing?.calibration || {};
    const rotation = activeDrawing?.product_rotation_deg ?? calibration.product_rotation_deg ?? 0;
    const tilt = activeDrawing?.product_tilt_deg ?? calibration.product_tilt_deg ?? 0;

    $('drawingProductRotation').value = String(rotation);
    $('drawingProductTilt').value = String(tilt);

    if ($('drawingPoseStatus'))
    {
        $('drawingPoseStatus').textContent =
            `제품 자세: 회전 ${Number(rotation).toFixed(1)}° / 틸팅 ${Number(tilt).toFixed(1)}°`;
    }
}


function drawingPoseValues()
{
    const rotation = Number($('drawingProductRotation')?.value ?? 0);
    const tilt = Number($('drawingProductTilt')?.value ?? 0);

    if (!Number.isFinite(rotation) || !Number.isFinite(tilt))
    {
        throw Error('제품 회전/틸팅 각도는 숫자로 입력하세요.');
    }
    if (Math.abs(rotation) > 360 || Math.abs(tilt) > 360)
    {
        throw Error('제품 회전/틸팅 각도는 -360° ~ 360° 범위여야 합니다.');
    }

    return {
        product_rotation_deg: rotation,
        product_tilt_deg: tilt
    };
}


function renderDrawingPanel()
{
    renderDrawingInfo();
    renderDrawingPointSets();
    renderDrawingPoints();
    renderDrawingRecipeSelector();
    renderPoints();
    window.dispatchEvent(new CustomEvent('drawing-cad-points', {
        detail: {drawing: activeDrawing}
    }));
}

function compatibleDrawingRecipes()
{
    return recipes.filter(recipe => !recipe.drawing?.point_file ||
        (recipe.drawing.product_id === activeDrawing?.product_id &&
         recipe.drawing.point_file === activeDrawing?.selected_point_file));
}

function drawingWorkflowError()
{
    return commands.length && !commands.includes('drawing.select_recipe')
        ? '실행 중인 PTM 서버가 이전 버전입니다. 최신 PTM 파일을 실행 장비에 반영하고 PTM 서버를 완전히 중지한 뒤 다시 실행하세요. 이후 브라우저에서 Ctrl+F5를 누르세요.'
        : '';
}

async function ensureDrawingWorkflow()
{
    if (!commands.length)
    {
        commands = (await cmd('system.commands', {}, true)).commands;
    }
    const error = drawingWorkflowError();
    if (error)
    {
        renderDrawingRecipeSelector();
        throw Error(error);
    }
}

async function activateDrawingRecipe(recipeId)
{
    await ensureDrawingWorkflow();
    const result = await cmd('drawing.select_recipe', {drawing_id: activeDrawing.id, recipe_id: recipeId}, true);
    if (!result.drawing?.selected_recipe_id)
    {
        throw Error('PTM 서버가 레시피 연결 정보를 반환하지 않았습니다. 최신 PTM 서버로 재시작하고 Ctrl+F5로 화면을 다시 불러오세요.');
    }
    activeDrawing = result.drawing;
    selectedDrawingPointId = activeDrawing.points?.[0]?.id || null;
}

function renderDrawingRecipeSelector()
{
    const select = $('drawingActiveRecipe');
    if (!select) return;
    const placeholder = new Option('레시피를 선택하세요', '');
    placeholder.disabled = true;
    select.replaceChildren(placeholder,
        ...compatibleDrawingRecipes().map(recipe => new Option(recipe.name, recipe.id)));
    select.value = activeDrawing?.selected_recipe_id || '';
    const serverError = drawingWorkflowError();
    if (serverError) $('drawingCalibrationInfo').textContent = '현재 서버에서 레시피별 캘리브레이션 연결을 확인할 수 없습니다. PTM 서버 재시작이 필요합니다.';
    select.disabled = !activeDrawing || Boolean(serverError);
    $('newDrawingRecipe').disabled = !activeDrawing || Boolean(serverError);
    const disabled = Boolean(serverError || (activeDrawing?.point_sets?.length && !activeDrawing.selected_recipe_id));
    const workflowStatus = $('drawingWorkflowStatus');
    if (workflowStatus) workflowStatus.textContent = serverError || (!activeDrawing
        ? '도면을 먼저 선택하세요.'
        : disabled ? '이 그룹의 레시피를 선택하거나 새 레시피를 만드세요. 연결이 완료되면 티칭·캘리브레이션 버튼이 활성화됩니다.'
        : activeDrawing.product_rotation_deg || activeDrawing.product_tilt_deg
            ? '각도 변환·저장은 가능합니다. 기준점 재티칭은 각도를 0°/0°로 되돌리고 캘리브레이션한 뒤 수행하세요.'
            : '레시피 연결 완료. 포인트를 선택한 후 현재값 적용·기준점 선택·캘리브레이션을 진행하세요.');
    for (const id of ['applyCurrentDrawingPoint', 'toggleCalibrationPoint', 'calibrateDrawing',
                      'resetDrawingCalibration', 'exportDrawingRecipe', 'moveDrawingPoint'])
    {
        $(id).disabled = !activeDrawing || disabled;
    }
    if (activeDrawing?.product_rotation_deg || activeDrawing?.product_tilt_deg)
    {
        $('applyCurrentDrawingPoint').disabled = true;
        $('toggleCalibrationPoint').disabled = true;
    }
    if (!selectedDrawingPoint()) $('applyCurrentDrawingPoint').disabled = true;
    updateDrawingCalibrationButton();
    updateDrawingMoveControls();
}

$('drawingActiveRecipe').onchange = drawingAction(async () => {
    const recipeId = $('drawingActiveRecipe').value;
    if (!recipeId) return false;
    $('drawingActiveRecipe').disabled = true;
    try
    {
        await activateDrawingRecipe(recipeId);
        loadDrawingPoseInputs();
        await loadRecipes(recipeId);
    }
    finally
    {
        renderDrawingPanel();
    }
}, {pending: '그룹 레시피 불러오는 중…', done: '그룹 레시피를 불러왔습니다.'});

$('newDrawingRecipe').onclick = drawingAction(async () => {
    await ensureDrawingWorkflow();
    const name = prompt('이 그룹에서 사용할 새 레시피 이름');
    if (!name?.trim()) return false;
    const created = await cmd('recipe.upsert', {name: name.trim()}, true);
    await activateDrawingRecipe(created.recipe.id);
    await loadRecipes(created.recipe.id);
    loadDrawingPoseInputs();
    renderDrawingPanel();
}, {pending: '새 레시피 생성 중…', done: '새 레시피를 생성했습니다.'});


function renderDrawingPointSets()
{
    const root = $('drawingPointSets');
    if (!root)
    {
        return;
    }
    root.replaceChildren();
    for (const pointSet of activeDrawing?.point_sets || [])
    {
        const label = document.createElement('label');
        label.className = 'cad-group';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'drawingPointSet';
        radio.value = pointSet.file;
        radio.checked = pointSet.file === activeDrawing.selected_point_file;
        radio.onchange = drawingAction(async () => {
            for (const item of document.querySelectorAll('input[name="drawingPointSet"]'))
            {
                item.disabled = true;
            }
            try
            {
                const result = await cmd('drawing.select_point_set', {
                    drawing_id: activeDrawing.id,
                    point_file: pointSet.file
                }, true);
                activeDrawing = result.drawing;
                selectedDrawingPointId = activeDrawing.points?.[0]?.id || null;
                loadDrawingPoseInputs();
            }
            finally
            {
                renderDrawingPanel();
            }
        }, {pending: '도면 포인트 그룹 불러오는 중…', done: '도면 포인트 그룹을 불러왔습니다.'});
        const text = document.createElement('span');
        text.textContent = `${pointSet.name || pointSet.file} · ${pointSet.point_count}개` +
            (pointSet.requires_process_review ? ' · 검토 필요' : '');
        label.append(radio, text);
        root.append(label);
    }
}


function renderDrawingInfo()
{
    if (!activeDrawing)
    {
        $('drawingInfo').textContent =
            '도면 없음';

        $('drawingCalibrationInfo').textContent =
            '도면 영역을 넓게 둘러싸는 최소 4개 포인트에 현재 Pan/Tilt를 적용한 뒤 캘리브레이션하세요.';

        return;
    }


    $('drawingInfo').textContent =
        `${activeDrawing.name} | Product ${activeDrawing.product_id} | ${activeDrawing.coordinate_frame} | ${activeDrawing.units} | ${activeDrawing.selected_point_file}`;


    const calibration =
        activeDrawing.calibration;

    $('drawingCalibrationInfo').textContent =
        calibration
            ? `캘리브레이션 완료: 제품 회전 ${Number(calibration.product_rotation_deg ?? 0).toFixed(1)}°, 틸팅 ${Number(calibration.product_tilt_deg ?? 0).toFixed(1)}° · 기준점 ${calibration.points_used}개 · Pan RMS ${calibration.rms_pan_error}°, Tilt RMS ${calibration.rms_tilt_error}°` +
              (calibration.rotation_axis ? ` · 회전축 ${calibration.rotation_axis}, 틸팅축 ${calibration.tilt_axis}` : '')
            : '도면 영역을 넓게 둘러싸는 기준점을 최소 4개 선택하세요. 먼저 제품 0°/0°에서 기준 캘리브레이션을 생성하세요.';
    updateDrawingSpeedLevel();
    updateDrawingMoveControls();
}


function renderDrawingPoints()
{
    const tbody =
        $('drawingPoints');

    if (!tbody)
    {
        return;
    }


    tbody.replaceChildren();

    for (const point of activeDrawing?.points || [])
    {
        const tr = document.createElement('tr');
        tr.className = 'drawing-point-row';
        tr.dataset.pointId = point.id;
        tr.tabIndex = -1;
        tr.classList.toggle('selected-point', point.id === selectedDrawingPointId);
        tr.classList.toggle('calibration-point', Boolean(point.calibration));
        tr.onclick = () => {
            selectedDrawingPointId = point.id;
            renderDrawingPoints();
            window.dispatchEvent(new CustomEvent('drawing-cad-select', {
                detail: {point_id: point.id}
            }));
        };
        const panInput = document.createElement('input');
        panInput.type = 'number';
        panInput.min = '0';
        panInput.max = '359.99';
        panInput.step = '.01';
        panInput.placeholder = 'Pan';
        panInput.value = point.pan ?? '';
        const tiltInput = document.createElement('input');
        tiltInput.type = 'number';
        tiltInput.min = '-60';
        tiltInput.max = '60';
        tiltInput.step = '.01';
        tiltInput.placeholder = 'Tilt';
        tiltInput.value = point.tilt ?? '';
        panInput.disabled = tiltInput.disabled = Boolean(
            (activeDrawing?.point_sets?.length && !activeDrawing.selected_recipe_id) ||
            activeDrawing?.product_rotation_deg || activeDrawing?.product_tilt_deg);
        const selectInputPoint = event => {
            event.stopPropagation();
            selectedDrawingPointId = point.id;
        };
        panInput.onclick = selectInputPoint;
        tiltInput.onclick = selectInputPoint;
        const saveAngles = guarded(async event => {
            event.stopPropagation();
            const result = await cmd('drawing.point_upsert', {
                drawing_id: activeDrawing.id,
                point_id: point.id,
                pan: nullableNumber(panInput.value),
                tilt: nullableNumber(tiltInput.value)
            }, true);
            activeDrawing = result.drawing;
            selectedDrawingPointId = point.id;
            renderDrawingPanel();
        });
        panInput.onchange = saveAngles;
        tiltInput.onchange = saveAngles;
        const values = [
            point.order,
            point.target_id || point.label,
            formatDrawingNumber(point.x),
            formatDrawingNumber(point.y),
            formatDrawingNumber(point.z),
            point.calibration ? '●' : '',
            panInput,
            tiltInput
        ];
        for (const value of values)
        {
            const td = document.createElement('td');
            if (value instanceof HTMLElement)
            {
                td.append(value);
            }
            else
            {
                td.textContent = value;
            }
            tr.append(td);
        }
        tbody.append(tr);
    }
    updateDrawingCalibrationButton();
    updateDrawingMoveControls();
    return;

    for (const point of activeDrawing?.points || [])
    {
        const tr =
            document.createElement('tr');

        tr.className =
            'drawing-point-row';

        tr.dataset.pointId =
            point.id;

        tr.classList.toggle(
            'selected-point',
            point.id === selectedDrawingPointId
        );

        tr.onclick =
            () => {

                selectedDrawingPointId =
                    point.id;

                renderDrawingPanel();
            };

        tr.ondragover =
            event => {

                if (
                    drawingDragPointId &&
                    drawingDragPointId !== point.id
                )
                {
                    event.preventDefault();

                    tr.classList.add(
                        'drop-target'
                    );
                }
            };

        tr.ondragleave =
            () =>
                tr.classList.remove(
                    'drop-target'
                );

        tr.ondrop =
            guarded(
                async event => {

                    event.preventDefault();

                    tr.classList.remove(
                        'drop-target'
                    );

                    await dropDrawingPoint(
                        point.id
                    );
                }
            );


        const handle =
            document.createElement('button');

        handle.type =
            'button';

        handle.className =
            'drag-handle';

        handle.draggable =
            true;

        handle.title =
            '드래그해서 순서 변경';

        handle.textContent =
            '↕';

        handle.onclick =
            event =>
                event.stopPropagation();

        handle.ondragstart =
            event => {

                drawingDragPointId =
                    point.id;

                event.dataTransfer.effectAllowed =
                    'move';

                event.dataTransfer.setData(
                    'text/plain',
                    point.id
                );

                tr.classList.add(
                    'dragging'
                );
            };

        handle.ondragend =
            () => {

                drawingDragPointId =
                    null;

                tr.classList.remove(
                    'dragging'
                );

                for (const row of tbody.querySelectorAll('.drop-target'))
                {
                    row.classList.remove(
                        'drop-target'
                    );
                }
            };


        const label =
            document.createElement('input');

        label.className =
            'drawing-label-input';

        label.value =
            point.label || `P${point.order}`;


        const calibration =
            document.createElement('input');

        calibration.type =
            'checkbox';

        calibration.checked =
            Boolean(point.calibration);


        const pan =
            document.createElement('input');

        pan.type =
            'number';

        pan.step =
            '.01';

        pan.min =
            '0';

        pan.max =
            '359.99';

        pan.value =
            point.pan ?? '';


        const tilt =
            document.createElement('input');

        tilt.type =
            'number';

        tilt.step =
            '.01';

        tilt.min =
            '-60';

        tilt.max =
            '60';

        tilt.value =
            point.tilt ?? '';


        const xyz =
            document.createElement('span');

        xyz.className =
            'drawing-xyz';

        xyz.textContent =
            `X ${formatDrawingNumber(point.x)} / Y ${formatDrawingNumber(point.y)} / Z ${formatDrawingNumber(point.z)}`;


        const angles =
            document.createElement('div');

        angles.className =
            'drawing-angle-fields';

        pan.setAttribute(
            'aria-label',
            'Pan'
        );

        tilt.setAttribute(
            'aria-label',
            'Tilt'
        );

        pan.placeholder =
            'Pan';

        tilt.placeholder =
            'Tilt';

        angles.append(
            pan,
            tilt
        );


        const save =
            button(
                '저장',

                async () => {

                    const result =
                        await cmd(
                            'drawing.point_upsert',

                            {
                                drawing_id:
                                    activeDrawing.id,

                                point_id:
                                    point.id,

                                label:
                                    label.value,

                                calibration:
                                    calibration.checked,

                                pan:
                                    nullableNumber(
                                        pan.value
                                    ),

                                tilt:
                                    nullableNumber(
                                        tilt.value
                                    )
                            },

                            true
                        );

                    activeDrawing =
                        result.drawing;

                    selectedDrawingPointId =
                        result.point.id;

                    renderDrawingPanel();
                }
            );

        const remove =
            button(
                '삭제',

                async () => {

                    if (
                        !confirm(
                            `${point.label || point.order} 삭제?`
                        )
                    )
                    {
                        return;
                    }


                    const result =
                        await cmd(
                            'drawing.point_delete',

                            {
                                drawing_id:
                                    activeDrawing.id,

                                point_id:
                                    point.id
                            },

                            true
                        );

                    activeDrawing =
                        result.drawing;

                    selectedDrawingPointId =
                        activeDrawing.points?.[0]?.id ||
                        null;

                    renderDrawingPanel();
                }
            );


        const cells = [
            handle,
            point.order,
            label,
            xyz,
            calibration,
            angles
        ];


        for (const value of cells)
        {
            const td =
                document.createElement('td');

            if (
                value instanceof HTMLElement
            )
            {
                td.append(value);
            }
            else
            {
                td.textContent =
                    value;
            }

            tr.append(td);
        }


        const actionCell =
            document.createElement('td');

        actionCell.className =
            'drawing-actions';

        actionCell.append(
            save,
            remove
        );

        tr.append(
            actionCell
        );

        tbody.append(
            tr
        );
    }
}


async function calibrateDrawing()
{
    if (!activeDrawing)
    {
        throw Error(
            '먼저 도면을 불러오세요.'
        );
    }

    const pose = drawingPoseValues();

    const result =
        await cmd(
            'drawing.calibrate',

            {
                drawing_id:
                    activeDrawing.id,
                ...pose
            }
        );

    activeDrawing =
        result.drawing;

    loadDrawingPoseInputs();
    renderDrawingPanel();

    const calibration = activeDrawing.calibration || {};
    $('notice').textContent =
        `제품 자세 보정 완료: 회전 ${Number(calibration.product_rotation_deg ?? pose.product_rotation_deg).toFixed(1)}° / ` +
        `틸팅 ${Number(calibration.product_tilt_deg ?? pose.product_tilt_deg).toFixed(1)}°`;
}


async function resetDrawingCalibration()
{
    if (!activeDrawing)
    {
        throw Error('먼저 도면을 선택하세요.');
    }
    if (!confirm(activeDrawing.selected_recipe_id
        ? '선택한 레시피의 Pan/Tilt 값과 캘리브레이션 기준을 지울까요?'
        : '이 도면의 모든 포인트 그룹에서 Pan/Tilt 값과 캘리브레이션 기준을 지울까요?'))
    {
        return false;
    }
    const result = await cmd('drawing.reset_calibration', {
        drawing_id: activeDrawing.id
    }, true);
    activeDrawing = result.drawing;
    loadDrawingPoseInputs();
    renderDrawingPanel();
    $('notice').textContent = '선택한 보정의 Pan/Tilt, 기준점, 제품 자세를 초기화했습니다.';
}


function focusDrawingPointRow(pointId)
{
    const row = Array.from(document.querySelectorAll('#drawingPoints tr'))
        .find(item => item.dataset.pointId === pointId);
    if (row)
    {
        row.focus({preventScroll: true});
        const panel = row.closest('.drawing-calibration-scroll');
        if (!panel) return;
        const top = panel.getBoundingClientRect().top + panel.clientTop;
        const bottom = top + panel.clientHeight;
        const bounds = row.getBoundingClientRect();
        const offset = bounds.top < top ? bounds.top - top
            : bounds.bottom > bottom ? bounds.bottom - bottom : 0;
        if (offset) panel.scrollBy({top: offset, behavior: 'smooth'});
    }
}


function selectedDrawingPoint()
{
    return activeDrawing?.points?.find(point => point.id === selectedDrawingPointId) || null;
}


async function applyCurrentDrawingPoint()
{
    const point = selectedDrawingPoint();
    const drawing = activeDrawing;
    if (!point)
    {
        throw Error('도면 또는 목록에서 포인트를 선택하세요.');
    }
    const current = await cmd('position.get', {refresh: true}, true);
    const pan = current.pan;
    const tilt = current.tilt;
    if (pan === null || pan === undefined || tilt === null || tilt === undefined)
    {
        throw Error('현재 Pan/Tilt 값을 먼저 조회하세요.');
    }
    status.position = {pan, tilt};
    const result = await cmd('drawing.point_upsert', {
        drawing_id: drawing.id,
        recipe_id: drawing.selected_recipe_id,
        point_id: point.id,
        pan,
        tilt
    }, true);
    activeDrawing = result.drawing;
    selectedDrawingPointId = result.point.id;
    $('notice').textContent =
        `${point.target_id || point.label}: Pan ${formatDrawingNumber(pan, 2)}°, Tilt ${formatDrawingNumber(tilt, 2)}° 적용`;
    renderDrawingPanel();
}


function updateDrawingMoveControls()
{
    const xyMode = $('drawingMoveXY');
    const move = $('moveDrawingPoint');
    if (!xyMode || !move)
    {
        return;
    }
    const calibrated = Boolean(activeDrawing?.calibration);
    xyMode.disabled = !calibrated;
    if (!calibrated)
    {
        xyMode.checked = false;
    }
    move.disabled = !selectedDrawingPoint() || Boolean(drawingWorkflowError() ||
        (activeDrawing?.point_sets?.length && !activeDrawing.selected_recipe_id));
}


async function moveSelectedDrawingPoint()
{
    const point = selectedDrawingPoint();
    if (!point)
    {
        throw Error('이동할 도면 포인트를 선택하세요.');
    }
    let pan = point.pan;
    let tilt = point.tilt;
    let mode = 'Pan/Tilt';
    if ($('drawingMoveXY').checked)
    {
        if (!activeDrawing.calibration)
        {
            throw Error('XY 좌표 이동은 캘리브레이션 완료 후에만 사용할 수 있습니다.');
        }
        const estimate = await cmd('drawing.estimate_pan_tilt', {
            drawing_id: activeDrawing.id,
            x: Number(point.x),
            y: Number(point.y),
            z: Number(point.z)
        }, true);
        pan = estimate.pan;
        tilt = estimate.tilt;
        mode = `XY (${formatDrawingNumber(point.x)}, ${formatDrawingNumber(point.y)})`;
    }
    if (pan === null || pan === undefined || tilt === null || tilt === undefined)
    {
        throw Error('해당 포인트에 Pan/Tilt 값이 없습니다. 현재값을 적용하거나 캘리브레이션하세요.');
    }
    await cmd('motion.absolute', {pan, tilt});
    $('notice').textContent =
        `${point.target_id || point.label}: ${mode} 이동 → Pan ${formatDrawingNumber(pan, 2)}°, Tilt ${formatDrawingNumber(tilt, 2)}°`;
}


function updateDrawingCalibrationButton()
{
    const button = $('toggleCalibrationPoint');
    if (!button)
    {
        return;
    }
    const point = selectedDrawingPoint();
    button.disabled = !point || Boolean(
        drawingWorkflowError() || (activeDrawing?.point_sets?.length && !activeDrawing.selected_recipe_id) ||
        activeDrawing?.product_rotation_deg || activeDrawing?.product_tilt_deg);
    button.textContent = point?.calibration
        ? '캘리브레이션 포인트 해제'
        : '캘리브레이션 포인트로 선택';
}


async function toggleCalibrationDrawingPoint()
{
    const point = selectedDrawingPoint();
    if (!point)
    {
        throw Error('도면 또는 목록에서 포인트를 선택하세요.');
    }
    if (!point.calibration && (point.pan === null || point.pan === undefined || point.tilt === null || point.tilt === undefined))
    {
        throw Error('현재값으로 적용한 뒤 캘리브레이션 포인트로 선택하세요.');
    }
    const calibration = !point.calibration;
    const result = await cmd('drawing.point_upsert', {
        drawing_id: activeDrawing.id,
        point_id: point.id,
        calibration
    }, true);
    activeDrawing = result.drawing;
    selectedDrawingPointId = result.point.id;
    $('notice').textContent = calibration
        ? `${point.target_id || point.label}: 캘리브레이션 포인트로 선택`
        : `${point.target_id || point.label}: 캘리브레이션 포인트 해제`;
    renderDrawingPanel();
}


async function estimateDrawingXY()
{
    if (!activeDrawing)
    {
        throw Error(
            '먼저 도면을 불러오세요.'
        );
    }


    const result =
        await cmd(
            'drawing.estimate_xy',

            {
                drawing_id:
                    activeDrawing.id,

                pan:
                    Number(
                        $('estimatePan').value
                    ),

                tilt:
                    Number(
                        $('estimateTilt').value
                    )
            },

            true
        );


    $('estimateResult').textContent =
        `추정 X=${result.x}, Y=${result.y} ${result.units} (${result.coordinate_frame}) | Z 미확정: ${result.z_reason} | RMS X=${result.rms_x_error}, Y=${result.rms_y_error}`;
}


let drawingRecipeMode = 'export';


function openDrawingRecipeDialog(mode)
{
    if (!activeDrawing)
    {
        throw Error('도면을 먼저 선택하세요.');
    }
    drawingRecipeMode = mode;
    $('drawingRecipeStatus').textContent = '';
    $('drawingRecipeDialogTitle').textContent =
        mode === 'export' ? '레시피로 저장' : '레시피에서 불러오기';
    $('drawingRecipeSelect').replaceChildren(
        ...compatibleDrawingRecipes().map(recipe => new Option(recipe.name, recipe.id))
    );
    $('drawingRecipeSelect').value = activeDrawing.selected_recipe_id || compatibleDrawingRecipes()[0]?.id || '';
    $('drawingNewRecipeLabel').hidden = mode !== 'export';
    $('drawingNewRecipeName').value = '';
    $('drawingRecipeDialog').showModal();
}


async function confirmDrawingRecipe()
{
    await ensureDrawingWorkflow();
    let recipeId = $('drawingRecipeSelect').value;
    const newName = $('drawingNewRecipeName').value.trim();
    if (drawingRecipeMode === 'export' && newName)
    {
        const created = await cmd('recipe.upsert', {name: newName}, true);
        recipeId = created.recipe.id;
        await loadRecipes(recipeId);
    }
    if (!recipeId)
    {
        throw Error('레시피를 선택하거나 새 레시피 이름을 입력하세요.');
    }
    if (drawingRecipeMode === 'export')
    {
        const result = await cmd('drawing.export_recipe', {
            drawing_id: activeDrawing.id,
            recipe_id: recipeId
        });
        await loadRecipes(result.recipe.id);
        await activateDrawingRecipe(result.recipe.id);
    }
    else
    {
        const result = await cmd('drawing.import_recipe', {
            drawing_id: activeDrawing.id,
            recipe_id: recipeId
        });
        activeDrawing = result.drawing;
    }
    $('drawingRecipeDialog').close();
    loadDrawingPoseInputs();
    renderDrawingPanel();
}


$('loadDrawing').onclick =
    drawingAction(
        () => loadDrawing(),
        {pending: '도면 불러오는 중…', done: '도면 준비가 완료되었습니다.', buttonId: 'loadDrawing'}
    );


$('drawingSelect').onchange =
    drawingAction(
        () => loadDrawing(),
        {pending: '도면 불러오는 중…', done: '도면 준비가 완료되었습니다.', buttonId: 'loadDrawing'}
    );

$('manageDrawings').onclick = drawingAction(openDrawingManager,
    {pending: '도면 목록 불러오는 중…', done: '도면 목록을 불러왔습니다.', statusId: 'drawingManagerStatus'});
$('uploadDrawing').onclick = drawingAction(uploadDrawingFile,
    {pending: '도면 파일 등록 중…', done: '도면 파일 등록이 완료되었습니다.', statusId: 'drawingManagerStatus'});


if ($('drawingRenderMode'))
{
    $('drawingRenderMode').onchange =
        event => {

            drawingView.renderMode =
                event.target.value;

            scheduleDrawingCanvas();
        };
}


if ($('drawingViewPreset'))
{
    $('drawingViewPreset').onchange =
        event => {

            applyDrawingViewPreset(
                event.target.value
            );
        };
}


if ($('drawingQuality'))
{
    $('drawingQuality').onchange =
        event => {

            drawingView.quality =
                event.target.value;

            scheduleDrawingCanvas();
        };
}


if ($('drawingHomeView'))
{
    $('drawingHomeView').onclick =
        resetDrawingView;
}


$('calibrateDrawing').onclick =
    drawingAction(
        calibrateDrawing,
        {pending: '캘리브레이션 중…', done: '캘리브레이션이 완료되었습니다.'}
    );


$('resetDrawingCalibration').onclick =
    drawingAction(
        resetDrawingCalibration,
        {pending: '보정 초기화 중…', done: '보정 초기화가 완료되었습니다.'}
    );


$('applyCurrentDrawingPoint').onclick =
    drawingAction(
        applyCurrentDrawingPoint,
        {pending: '현재 Pan/Tilt 값 적용 중…', done: '현재 Pan/Tilt 값을 적용했습니다.'}
    );


$('moveDrawingPoint').onclick =
    drawingAction(
        moveSelectedDrawingPoint,
        {pending: '선택 포인트 이동 요청 중…', done: '선택 포인트 이동 명령을 전달했습니다.'}
    );


$('toggleCalibrationPoint').onclick =
    drawingAction(
        toggleCalibrationDrawingPoint,
        {pending: '기준 포인트 설정 중…', done: '기준 포인트 설정을 저장했습니다.'}
    );


$('exportDrawingRecipe').onclick =
    guarded(
        () => openDrawingRecipeDialog('export')
    );


$('importDrawingRecipe').onclick =
    guarded(
        () => openDrawingRecipeDialog('import')
    );


$('confirmDrawingRecipe').onclick =
    drawingAction(
        confirmDrawingRecipe,
        () => ({pending: drawingRecipeMode === 'export' ? '레시피로 내보내는 중…' : '레시피에서 불러오는 중…',
            done: drawingRecipeMode === 'export' ? '레시피로 내보내기가 완료되었습니다.' : '레시피에서 불러오기가 완료되었습니다.',
            statusId: 'drawingRecipeStatus'})
    );


window.addEventListener('drawing-cad-point-selected', event => {
    selectedDrawingPointId = event.detail?.point_id || null;
    renderDrawingPoints();
    requestAnimationFrame(() => focusDrawingPointRow(selectedDrawingPointId));
});


if ($('drawingCanvas'))
{
    $('drawingCanvas').addEventListener(
        'contextmenu',
        event =>
            event.preventDefault()
    );

    $('drawingCanvas').addEventListener(
        'pointerdown',

        event => {

            $('drawingCanvas').setPointerCapture(
                event.pointerId
            );

            drawingView.dragging =
                true;

            drawingView.interacting =
                true;

            drawingView.button =
                event.button;

            drawingView.moved =
                false;

            drawingView.lastX =
                event.clientX;

            drawingView.lastY =
                event.clientY;
        }
    );

    $('drawingCanvas').addEventListener(
        'pointermove',

        event => {

            if (!drawingView.dragging)
            {
                return;
            }


            const dx =
                event.clientX - drawingView.lastX;

            const dy =
                event.clientY - drawingView.lastY;


            if (
                Math.hypot(dx, dy) > 2
            )
            {
                drawingView.moved =
                    true;
            }


            if (
                drawingView.button === 0
            )
            {
                drawingView.yaw +=
                    dx * 0.45;

                drawingView.pitch =
                    Math.max(
                        -89,
                        Math.min(
                            89,
                            drawingView.pitch + dy * 0.45
                        )
                    );
            }
            else
            {
                drawingView.panX +=
                    dx * (window.devicePixelRatio || 1);

                drawingView.panY +=
                    dy * (window.devicePixelRatio || 1);
            }


            drawingView.lastX =
                event.clientX;

            drawingView.lastY =
                event.clientY;

            scheduleDrawingCanvas();
        }
    );

    $('drawingCanvas').addEventListener(
        'pointerup',

        guarded(
            async event => {

                const shouldPick =
                    drawingView.dragging &&
                    drawingView.button === 0 &&
                    !drawingView.moved;

                drawingView.dragging =
                    false;

                drawingView.interacting =
                    false;

                scheduleDrawingCanvas();

                if (!shouldPick)
                {
                    return;
                }


                const picked =
                    pickDrawingPosition(
                        canvasPoint(event)
                    );

                if (picked)
                {
                    await addDrawingPoint(
                        picked
                    );
                }
            }
        )
    );

    $('drawingCanvas').addEventListener(
        'wheel',

        event => {

            event.preventDefault();

            drawingView.zoom =
                Math.max(
                    0.08,
                    Math.min(
                        60,
                        drawingView.zoom *
                            (
                                event.deltaY < 0
                                    ? 1.12
                                    : 1 / 1.12
                            )
                    )
                );

            drawingView.interacting =
                true;

            scheduleDrawingCanvas();

            clearTimeout(
                drawingView.wheelTimer
            );

            drawingView.wheelTimer =
                setTimeout(
                    () => {

                        drawingView.interacting =
                            false;

                        scheduleDrawingCanvas();
                    },
                    180
                );
        },

        {
            passive: false
        }
    );

    window.addEventListener(
        'resize',
        scheduleDrawingCanvas
    );
}


// ============================================================================
// API Command UI
// ============================================================================

const enumFields = {

    ...(window.protocolEnums || {}),

    pan: [
        'stop',
        'left',
        'right'
    ],

    tilt: [
        'stop',
        'up',
        'down'
    ],

    direction: [
        'faster',
        'slower'
    ],

    point: [
        'start',
        'end'
    ]
};


function selectCommands()
{
    const filtered = commands.filter(command =>
        visibleProtocolCommand(command) &&
        (!selectedCommandGroup || command.startsWith(selectedCommandGroup + '.'))
    );
    if (!filtered.includes(selectedCommandName))
    {
        selectedCommandName = filtered[0] || '';
    }
    $('commandList').replaceChildren(...filtered.map(command => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.command = command;
        button.classList.toggle('active', command === selectedCommandName);
        const name = document.createElement('strong');
        name.textContent = command;
        const description = document.createElement('span');
        description.textContent = protocolCommandGuide[command] || '이 명령의 매개변수를 입력해 실행합니다.';
        button.append(name, description);
        button.onclick = () => {
            selectedCommandName = command;
            showCommand();
        };
        return button;
    }));
    showCommand();
}


function showCommand()
{
    const command = selectedCommandName;
    if (!command)
    {
        return;
    }
    for (const button of document.querySelectorAll('#commandList button'))
    {
        button.classList.toggle('active', button.dataset.command === command);
    }
    $('commandTitle').textContent = command;
    $('commandDescription').textContent = protocolCommandGuide[command] ||
        `${protocolGroupGuide[command.split('.')[0]] || '고급 API 명령입니다.'} 장치별 지원 여부와 입력값을 확인하세요.`;


    const params = {
        ...(templates[command] || {})
    };


    if (
        'recipe_id' in params
    )
    {
        params.recipe_id =
            selected()?.id
            || '';
    }


    if (
        'point_id' in params
    )
    {
        params.point_id =
            selected()?.points[0]?.id
            || '';
    }


    if (
        'drawing_id' in params
    )
    {
        params.drawing_id =
            activeDrawingId();
    }


    $('commandFields')
        .replaceChildren();


    for (
        const [key, value]
        of Object.entries(params)
    )
    {
        const label =
            document.createElement(
                'label'
            );


        label.textContent =
            key;


        let input;


        if (
            enumFields[key] &&
            typeof value === 'string'
        )
        {
            input =
                document.createElement(
                    'select'
                );


            for (
                const option
                of enumFields[key]
            )
            {
                input.add(
                    new Option(
                        option,
                        option
                    )
                );
            }


            input.value =
                value;
        }

        else
        {
            input =
                document.createElement(
                    'input'
                );


            input.type =
                typeof value === 'boolean'
                    ? 'checkbox'
                    : typeof value === 'number'
                        ? 'number'
                        : 'text';


            if (
                input.type ===
                'checkbox'
            )
            {
                input.checked =
                    key === 'confirm'
                        ? false
                        : value;
            }

            else
            {
                input.value =
                    typeof value === 'object'
                        ? JSON.stringify(value)
                        : value;
            }


            if (
                input.type === 'number'
            )
            {
                input.step =
                    'any';
            }
        }


        input.name =
            key;


        input.dataset.json =
            String(
                typeof value ===
                'object'
            );


        label.append(input);


        $('commandFields')
            .append(label);
    }


    updateJson();
}


function commandParams()
{
    const params =
        formData(
            $('commandForm')
        );


    for (
        const element
        of $('commandForm').elements
    )
    {
        if (
            element.dataset.json ===
            'true'
        )
        {
            params[element.name] =
                JSON.parse(
                    element.value
                );
        }
    }


    return params;
}


function updateJson()
{
    $('jsonParams').value =
        JSON.stringify(
            commandParams(),
            null,
            2
        );
}


$('commandForm').oninput =
    guarded(updateJson);


async function runCommand(params)
{
    const command = selectedCommandName;


    if (
        params.confirm === true &&
        !confirm(
            command +
            ' 명령을 실행할까요?'
        )
    )
    {
        return;
    }


    const result =
        await cmd(
            command,
            params
        );


    $('commandResult').textContent =
        JSON.stringify(
            result,
            null,
            2
        );


    if (
        command.startsWith('recipe.') ||
        command.startsWith('point.')
    )
    {
        await loadRecipes();
    }
}


$('commandForm').onsubmit =
    guarded(
        async event => {

            event.preventDefault();

            await runCommand(
                commandParams()
            );
        }
    );


$('sendJson').onclick =
    guarded(
        () =>
            runCommand(
                JSON.parse(
                    $('jsonParams').value
                )
            )
    );


async function loadCommands()
{
    commands =
        (
            await cmd(
                'system.commands',
                {},
                true
            )
        ).commands;

    renderDrawingRecipeSelector();


    const groups =
        [
            ...new Set(
                commands.filter(visibleProtocolCommand).map(
                    command =>
                        command.split('.')[0]
                )
            )
        ];


    const groupNames = ['', ...groups];
    $('commandGroups').replaceChildren(...groupNames.map(group => {
        const button = document.createElement('button');
        button.type = 'button';
        button.classList.toggle('active', group === selectedCommandGroup);
        const name = document.createElement('strong');
        name.textContent = group || '전체';
        const description = document.createElement('span');
        description.textContent = group
            ? (protocolGroupGuide[group] || `${group} 명령 모음`)
            : '지원되는 모든 명령을 표시합니다.';
        button.append(name, description);
        button.onclick = () => {
            selectedCommandGroup = group;
            for (const item of document.querySelectorAll('#commandGroups button'))
            {
                item.classList.toggle('active', item === button);
            }
            selectCommands();
        };
        return button;
    }));
    selectedCommandName = commands.includes('system.ping') ? 'system.ping' : commands[0] || '';
    selectCommands();
}


// ============================================================================
// 로그 버튼
// ============================================================================

$('clearLogs').onclick =
    () => {

        logs = [];

        renderLogs();
    };


$('exportLogs').onclick =
    () => {

        const url =
            URL.createObjectURL(

                new Blob(
                    [
                        logs
                            .map(
                                item =>
                                    JSON.stringify(
                                        item
                                    )
                            )
                            .join('\n')
                    ],

                    {
                        type:
                            'application/x-ndjson'
                    }
                )
            );


        const anchor =
            document.createElement(
                'a'
            );


        anchor.href =
            url;


        anchor.download =
            'pt503-log.jsonl';


        anchor.click();


        setTimeout(
            () =>
                URL.revokeObjectURL(
                    url
                ),

            1000
        );
    };


// ============================================================================
// GAMEPAD 기본 함수
// ============================================================================

const padActions = {
    none: '사용 안 함', stop: '강제 정지', goto: '선택 포인트 이동', home: '0°, 0° 이동',
    laser: '레이저 ON/OFF', laser_on: '레이저 ON', laser_off: '레이저 OFF',
    recipe_prev: '이전 레시피', recipe_next: '다음 레시피',
    point_prev: '이전 포인트', point_next: '다음 포인트',
    speed_down: '속도 감소', speed_up: '속도 증가',
    pan_left: 'Pan 왼쪽 (누르는 동안)', pan_right: 'Pan 오른쪽 (누르는 동안)',
    tilt_up: 'Tilt 위 (누르는 동안)', tilt_down: 'Tilt 아래 (누르는 동안)'
};
let padDisconnecting = false;

function browserPads()
{
    return [...(navigator.getGamepads?.() || [])].filter(pad => pad && pad.connected !== false);
}

function selectedPad()
{
    return padIndex === null ? null : browserPads().find(pad => pad.index === padIndex) || null;
}

function defaultPadMapping(pad)
{
    const standard = pad.mapping === 'standard';
    const buttons = {};
    const defaults = standard
        ? {0: 'goto', 1: 'stop', 2: 'home', 3: 'laser', 4: 'recipe_prev', 5: 'recipe_next',
            6: 'point_prev', 7: 'point_next', 14: 'speed_down', 15: 'speed_up'}
        : {[PAD_BUTTON.A]: 'goto', [PAD_BUTTON.B]: 'stop', [PAD_BUTTON.X]: 'home',
            [PAD_BUTTON.Y]: 'laser', [PAD_BUTTON.LB]: 'recipe_prev', [PAD_BUTTON.RB]: 'recipe_next',
            [PAD_BUTTON.LT]: 'point_prev', [PAD_BUTTON.RT]: 'point_next'};
    pad.buttons.forEach((_, index) => { buttons['b' + index] = defaults[index] || 'none'; });
    if (!standard) Object.assign(buttons, {p0: 'none', p2: 'speed_up', p4: 'none', p6: 'speed_down'});
    return {pan: standard ? 2 : PAD_AXIS.RIGHT_X, tilt: standard ? 3 : PAD_AXIS.RIGHT_Y,
        pov: standard ? -1 : PAD_AXIS.POV, buttons};
}

function loadPadMapping(pad)
{
    padMappingKey = 'ptm.gamepad.mapping.v1:' + pad.mapping + ':' + pad.id;
    padMapping = defaultPadMapping(pad);
    try
    {
        const saved = JSON.parse(localStorage.getItem(padMappingKey) || 'null');
        if (saved && typeof saved === 'object')
        {
            for (const key of ['pan', 'tilt', 'pov'])
            {
                if (Number.isInteger(saved[key]) && saved[key] >= -1 && saved[key] < pad.axes.length)
                    padMapping[key] = saved[key];
            }
            for (const key of Object.keys(padMapping.buttons))
            {
                if (Object.hasOwn(padActions, saved.buttons?.[key])) padMapping.buttons[key] = saved.buttons[key];
            }
        }
    }
    catch { /* 저장값이 없거나 손상되면 기본 매핑을 사용합니다. */ }
    padMappingSignature = '';
    renderPadMapping(pad);
}

function savePadMapping()
{
    try
    {
        localStorage.setItem(padMappingKey, JSON.stringify(padMapping));
        $('padStatus').textContent = '게임패드 매핑 저장됨';
    }
    catch { $('padStatus').textContent = '브라우저 저장 불가 / 현재 연결에 매핑 적용됨'; }
}

function padInputLabel(pad, key)
{
    if (key.startsWith('p')) return '방향키 ' + ({p0: '위', p2: '오른쪽', p4: '아래', p6: '왼쪽'})[key];
    const index = Number(key.slice(1));
    const labels = pad.mapping === 'standard'
        ? ['A', 'B', 'X', 'Y', 'LB', 'RB', 'LT', 'RT', 'BACK', 'START', 'L3', 'R3', '방향키 위', '방향키 아래', '방향키 왼쪽', '방향키 오른쪽', 'HOME']
        : ['Y', 'B', 'A', 'X', 'LB', 'RB', 'LT', 'RT', 'BACK', 'START', 'L3', 'R3'];
    return `버튼 ${index}${labels[index] ? ' / ' + labels[index] : ''}`;
}

function renderPadMapping(pad)
{
    const signature = `${pad.buttons.length}/${pad.axes.length}`;
    if (padMappingSignature === signature) return;
    padMappingSignature = signature;
    const defaults = defaultPadMapping(pad);
    padMapping.buttons = Object.fromEntries(Object.entries(defaults.buttons).map(([key, value]) =>
        [key, padMapping.buttons[key] || value]));
    for (const [id, key] of [['padX', 'pan'], ['padY', 'tilt'], ['padPov', 'pov']])
    {
        $(id).replaceChildren(new Option('사용 안 함', -1),
            ...pad.axes.map((_, index) => new Option('Axis ' + index, index)));
        $(id).value = String(padMapping[key] < pad.axes.length ? padMapping[key] : -1);
        padMapping[key] = Number($(id).value);
        $(id).onchange = guarded(async () => {
            const value = Number($(id).value);
            const mapping = padMapping;
            await stopJog(true);
            if (mapping !== padMapping) return;
            padMapping[key] = value;
            oldPadInputs = readPadInputs(pad).inputs;
            savePadMapping();
        });
    }
    $('padPovField').hidden = pad.mapping === 'standard';
    $('padMappings').replaceChildren(...Object.keys(padMapping.buttons).map(key => {
        const row = document.createElement('tr');
        row.dataset.input = key;
        const label = document.createElement('td');
        label.textContent = padInputLabel(pad, key);
        const value = document.createElement('td');
        value.textContent = '0.00';
        const field = document.createElement('td');
        const select = document.createElement('select');
        select.setAttribute('aria-label', label.textContent + ' 동작');
        select.replaceChildren(...Object.entries(padActions).map(([action, text]) => new Option(text, action)));
        select.value = padMapping.buttons[key];
        select.onchange = guarded(async () => {
            const action = select.value;
            const mapping = padMapping;
            await stopJog(true);
            if (mapping !== padMapping) return;
            padMapping.buttons[key] = action;
            oldPadInputs = readPadInputs(pad).inputs;
            savePadMapping();
        });
        field.append(select);
        row.append(label, value, field);
        return row;
    }));
}

function readPadInputs(pad)
{
    const inputs = {};
    const values = {};
    pad.buttons.forEach((button, index) => {
        inputs['b' + index] = button.pressed || button.value > 0.5;
        values['b' + index] = Number(button.value || 0);
    });
    const pov = pad.mapping === 'standard' || padMapping.pov < 0
        ? 15 : decodePovAxis(pad.axes[padMapping.pov]);
    if (pad.mapping !== 'standard')
    {
        for (const direction of [0, 2, 4, 6])
        {
            inputs['p' + direction] = pov !== 15 &&
                [direction, (direction + 1) % 8, (direction + 7) % 8].includes(pov);
            values['p' + direction] = Number(inputs['p' + direction]);
        }
    }
    return {inputs, values};
}

function refreshGamepads()
{
    const pads = browserPads();
    const current = $('padSelect').value;
    $('padSelect').replaceChildren(...pads.map(pad => new Option(`${pad.index}: ${pad.id}`, pad.index)));
    if (pads.some(pad => String(pad.index) === current)) $('padSelect').value = current;
    $('padStatus').textContent = pads.length ? `${pads.length}개 감지됨` : '감지된 게임패드 없음';
    if (!padConnected) $('padName').textContent = pads[0]?.id || '연결된 게임패드 없음';
}

async function connectGamepad()
{
    if (padDisconnecting) return;
    const selected = $('padSelect').value;
    refreshGamepads();
    const pad = browserPads().find(pad => String(pad.index) === selected) || browserPads()[0];
    if (!pad) throw Error('패드 버튼을 한 번 누른 뒤 감지해보세요.');
    if (padConnected) await stopJog();
    padAutoConnect = true;
    padIndex = pad.index;
    padConnected = true;
    $('padSelect').value = String(padIndex);
    $('padEnable').checked = true;
    loadPadMapping(pad);
    oldPadInputs = readPadInputs(pad).inputs;
    $('padName').textContent = pad.id;
    $('padStatus').textContent = '게임패드 연결됨 / 스틱·버튼을 놓으세요.';
    padActive = false;
    padRequireCenter = true;
}

async function disconnectGamepad(manual = true)
{
    if (padDisconnecting) return;
    padDisconnecting = true;
    padAutoConnect = false;
    padConnected = false;
    padIndex = null;
    $('padEnable').checked = false;
    try { await stopJog(); }
    finally
    {
        oldPadInputs = {};
        $('padStatus').textContent = manual ? '해제됨' : '연결 끊김 / 자동 재연결 대기';
        $('padName').textContent = '연결된 게임패드 없음';
        padAutoConnect = !manual;
        padDisconnecting = false;
    }
}

// ============================================================================
// Recipe / Point Gamepad 선택
// ============================================================================

function stepRecipe(delta)
{
    if (
        !$('recipeSelect')
            .options
            .length
    )
    {
        return;
    }


    const count =
        $('recipeSelect')
            .options
            .length;


    const index =
        $('recipeSelect')
            .selectedIndex;


    $('recipeSelect')
        .selectedIndex =
        (
            index +
            delta +
            count
        ) % count;


    selectedPointId =
        null;


    clearPoint();

    renderPoints();


    $('padStatus').textContent =
        '레시피 선택: '
        + (
            selected()?.name
            || '-'
        );
}


function stepPoint(delta)
{
    const recipe =
        selected();


    const points =
        recipe?.points
        || [];


    if (!points.length)
    {
        $('padStatus').textContent =
            '선택 가능한 포인트 없음';

        return;
    }


    let current =
        points.findIndex(
            point =>
                point.id ===
                selectedPointId
        );


    if (
        current < 0
    )
    {
        current = 0;
    }


    const next =
        (
            current +
            delta +
            points.length
        ) % points.length;


    selectedPointId =
        points[next].id;


    renderPoints();


    $('padStatus').textContent =
        '포인트 선택: '
        + (
            selectedPoint()?.name
            || '-'
        );
}


// ============================================================================
// A 버튼
// ============================================================================

async function gotoSelectedPoint()
{
    const recipe =
        selected();


    const point =
        selectedPoint();


    if (
        !recipe ||
        !point
    )
    {
        throw Error(
            '선택된 레시피/포인트 없음'
        );
    }


    await stopJog(true);


    await cmd(
        'point.goto',

        {
            recipe_id:
                recipe.id,

            point_id:
                point.id
        }
    );
}


// ============================================================================
// X 버튼
// ============================================================================

async function gotoHome()
{
    await stopJog(true);


    await cmd(
        'motion.absolute',

        {
            pan: 0,

            tilt: 0
        }
    );
}


// ============================================================================
// Y 버튼 - Laser Toggle
// ============================================================================

async function toggleLaser()
{
    if (
        status.laser?.on
    )
    {
        await cmd(
            'laser.off'
        );

        return;
    }


    await cmd(
        'laser.on', {manual: true}
    );
}


// ============================================================================
// Gamepad 이벤트
// ============================================================================

$('padRefresh').onclick =
    refreshGamepads;


$('padConnect').onclick =
    guarded(
        connectGamepad
    );


$('padDisconnect').onclick = guarded(() => disconnectGamepad());
$('padEnable').onchange = guarded(async () => {
    if ($('padEnable').checked && !padConnected) await connectGamepad();
    else if (!$('padEnable').checked) await stopJog();
});
$('padMappingReset').onclick = guarded(async () => {
    const pad = selectedPad();
    if (!pad) throw Error('게임패드를 먼저 연결하세요.');
    await stopJog(true);
    padMapping = defaultPadMapping(pad);
    padMappingSignature = '';
    renderPadMapping(pad);
    oldPadInputs = readPadInputs(pad).inputs;
    savePadMapping();
});
window.addEventListener('gamepadconnected', guarded(async () => {
    padAutoConnect = true;
    refreshGamepads();
    if (!padConnected) await connectGamepad();
}));
window.addEventListener('gamepaddisconnected', guarded(async () => {
    if (padConnected && !selectedPad()) await disconnectGamepad(false);
    refreshGamepads();
}));

// ============================================================================
// 진동 테스트 버튼
// ============================================================================

$('gamepad').append(

    button(
        '진동 테스트',

        async () => {

            const pad =
                selectedPad()
                || browserPads()[0];


            if (
                !pad?.vibrationActuator
            )
            {
                throw Error(
                    '이 패드 또는 브라우저는 '
                    + '진동을 지원하지 않습니다.'
                );
            }


            await pad
                .vibrationActuator
                .playEffect(
                    'dual-rumble',

                    {
                        duration: 300,

                        strongMagnitude:
                            0.7,

                        weakMagnitude:
                            0.25
                    }
                );
        }
    )
);


// ============================================================================
// D-Pad POV Decode
// ============================================================================
//
// 브라우저에서 POV가 다음처럼 들어오는 게임패드:
//
// POV 0 = -1.000000
// POV 1 = -0.714285
// POV 2 = -0.428571
// POV 3 = -0.142857
// POV 4 =  0.142857
// POV 5 =  0.428571
// POV 6 =  0.714285
// POV 7 =  1.000000
//
// 중립 POV 15:
//
// 3.285714
//
// ============================================================================

function decodePovAxis(value)
{
    if (
        !Number.isFinite(value)
    )
    {
        return 15;
    }


    const pov =
        Math.round(
            (value + 1) * 3.5
        );


    if (
        pov >= 0 &&
        pov <= 7
    )
    {
        return pov;
    }


    return 15;
}


function povName(pov)
{
    switch (pov)
    {
        case 0:
            return '↑ UP';

        case 1:
            return '↗ UP + RIGHT';

        case 2:
            return '→ RIGHT';

        case 3:
            return '↘ DOWN + RIGHT';

        case 4:
            return '↓ DOWN';

        case 5:
            return '↙ DOWN + LEFT';

        case 6:
            return '← LEFT';

        case 7:
            return '↖ UP + LEFT';

        default:
            return 'CENTER';
    }
}


// ============================================================================
// 속도 단계 변경
// ============================================================================

function setSpeedStep(
    id,
    delta
)
{
    const element =
        $(id);


    const current =
        Number(
            element.value
        );


    const next =
        Math.max(
            1,

            Math.min(
                8,

                current +
                delta
            )
        );


    element.value =
        String(next);


    localStorage.setItem(
        id,
        element.value
    );
    updateDrawingSpeedLevel();
}


function renderGamepadMonitor(pad, rightX, rightY, inputs, values)
{
    for (const row of $('padMappings').querySelectorAll('tr'))
    {
        row.classList.toggle('pressed', Boolean(inputs[row.dataset.input]));
        row.cells[1].textContent = (values[row.dataset.input] || 0).toFixed(2);
    }
    $('padValues').textContent = [
        `Pan Axis ${padMapping.pan}: ${rightX.toFixed(3)} / Tilt Axis ${padMapping.tilt}: ${rightY.toFixed(3)}`,
        '전체 축: ' + pad.axes.map((value, index) => `${index}=${value.toFixed(3)}`).join('  '),
        `현재 이동: ${jog ? `Pan=${jog.pan}, Tilt=${jog.tilt}` : 'STOP'}`,
        `통합 속도: ${$('speedLevel').value} / 8`,
        `레시피: ${selected()?.name || '-'} / 포인트: ${selectedPoint()?.name || '-'}`,
        `레이저: ${status.laser?.on ? 'ON' : 'OFF'}`,
        `중앙·버튼 해제 대기: ${padRequireCenter ? 'YES' : 'NO'}`
    ].join('\n');
}

async function runPadAction(action)
{
    switch (action)
    {
        case 'goto': await gotoSelectedPoint(); break;
        case 'home': await gotoHome(); break;
        case 'laser': await toggleLaser(); break;
        case 'laser_on': await cmd('laser.on', {manual: true}); break;
        case 'laser_off': await cmd('laser.off'); break;
        case 'recipe_prev': stepRecipe(-1); break;
        case 'recipe_next': stepRecipe(1); break;
        case 'point_prev': stepPoint(-1); break;
        case 'point_next': stepPoint(1); break;
        case 'speed_down': setSpeedStep('speedLevel', -1); break;
        case 'speed_up': setSpeedStep('speedLevel', 1); break;
    }
}

function pollGamepad()
{
    if (padDisconnecting) return;
    if (jogStopBusy)
    {
        padRequireCenter = true;
        return;
    }
    if (!padConnected && padAutoConnect && browserPads().length)
    {
        guarded(connectGamepad)();
        return;
    }
    const pad = selectedPad();
    if (!pad)
    {
        if (padConnected) guarded(() => disconnectGamepad(false))();
        $('padValues').textContent = '게임패드 입력 없음';
        return;
    }
    renderPadMapping(pad);
    const {inputs, values} = readPadInputs(pad);
    let rightX = pad.axes[padMapping.pan] || 0;
    let rightY = pad.axes[padMapping.tilt] || 0;
    for (const [key, pressed] of Object.entries(inputs))
    {
        if (!pressed) continue;
        switch (padMapping.buttons[key])
        {
            case 'pan_left': rightX -= 1; break;
            case 'pan_right': rightX += 1; break;
            case 'tilt_up': rightY -= 1; break;
            case 'tilt_down': rightY += 1; break;
        }
    }
    renderGamepadMonitor(pad, rightX, rightY, inputs, values);
    const previous = oldPadInputs;
    oldPadInputs = {...inputs};
    if (!$('padEnable').checked || document.hidden || !document.hasFocus() || !status.serial?.connected)
    {
        padRequireCenter = true;
        if (padActive) guarded(stopJog)();
        return;
    }
    const stops = Object.keys(inputs).filter(key => inputs[key] && padMapping.buttons[key] === 'stop');
    if (stops.length)
    {
        if (stops.some(key => !previous[key]) || padActive) guarded(stopJog)();
        padRequireCenter = true;
        return;
    }
    if (recipeTestRunning || status.operation?.test_active)
    {
        padRequireCenter = true;
        return;
    }
    const xActive = Math.abs(rightX) >= PAD_DEADZONE;
    const yActive = Math.abs(rightY) >= PAD_DEADZONE;
    const centered = !xActive && !yActive;
    if (padRequireCenter)
    {
        if (centered && !Object.values(inputs).some(Boolean)) padRequireCenter = false;
        return;
    }
    const actions = [...new Set(Object.keys(inputs)
        .filter(key => inputs[key] && !previous[key])
        .map(key => padMapping.buttons[key]))];
    for (const action of actions) guarded(() => runPadAction(action))();
    if (actions.includes('goto') || actions.includes('home')) return;
    if (centered)
    {
        if (padActive)
        {
            const now = performance.now();
            if (!padCenteredSince) padCenteredSince = now;
            else if (now - padCenteredSince >= PAD_CENTER_STOP_DELAY_MS)
            {
                jog = null;
                padActive = false;
                padCenteredSince = 0;
                guarded(() => cmd('motion.stop', {preserve_laser_manual: true}, true))();
            }
        }
        else padCenteredSince = 0;
        padCentered = true;
    }
    else
    {
        padCenteredSince = 0;
        const nextJog = {
            pan: !xActive ? 'stop' : rightX > 0 ? 'right' : 'left',
            tilt: !yActive ? 'stop' : rightY < 0 ? 'up' : 'down'
        };
        const changed = !jog || jog.pan !== nextJog.pan || jog.tilt !== nextJog.tilt;
        jog = nextJog;
        padActive = true;
        padCentered = false;
        if (changed) guarded(sendJog)();
    }
}

setInterval(pollGamepad, 60);

// ============================================================================
// 초기 실행
// ============================================================================

refreshGamepads();


Promise.allSettled([
    ports(),
    loadRecipes(),
    loadCommands(),
    (async () => {
        await loadDrawings();
        // CAD 모듈의 이벤트 등록이 끝난 후 첫 도면을 불러옵니다.
        if (document.readyState === 'loading')
            await new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, {once: true}));
        if (drawings.length && !activeDrawing)
            await drawingAction(() => loadDrawing(drawings[0].id),
                {pending: '첫 도면 불러오는 중…', done: '도면 준비가 완료되었습니다.', buttonId: 'loadDrawing'})();
    })()
]);


refresh();

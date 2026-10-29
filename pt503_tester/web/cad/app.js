import {createStandaloneViewer, MODEL_UNITS_PER_MM} from './viewer.js';

const byId = id => document.getElementById(id);
const state = {
    drawing: null,
    points: [],
    viewer: null,
    modelUrl: '',
    selectedPointId: null
};

function viewerPoints(points)
{
    return (points || []).map(point => ({
        ...point,
        group_id: Number(point.group_id),
        target_id: String(point.target_id || point.id),
        xyz_mm: [Number(point.x), Number(point.y), Number(point.z)]
    }));
}

function pointReview(point)
{
    return point.possible_non_fastening_features ? '검토 필요' : '후보';
}

function showPoint(point, notify = true)
{
    state.selectedPointId = point?.id || null;
    if (!point)
    {
        byId('cadSelection').textContent = '도면 포인트를 선택하세요.';
        return;
    }
    byId('cadSelection').textContent = [
        `식별 키: (${point.group_id}, ${point.target_id})`,
        `CAD XYZ: ${point.xyz_mm.join(', ')} ${state.drawing.units}`,
        `좌표계: ${state.drawing.coordinate_frame}`,
        `상태: ${point.status || '-'} / ${pointReview(point)}`,
        `Pan/Tilt: ${point.pan ?? '-'} / ${point.tilt ?? '-'}`,
        `contact-Z 인증: ${point.contact_z_certified ? '인증됨' : '미인증 (실제 체결 깊이 사용 금지)'}`
    ].join('\n');
    if (notify)
    {
        window.dispatchEvent(new CustomEvent('drawing-cad-point-selected', {
            detail: {point_id: point.id}
        }));
    }
}

function validateDrawing(drawing)
{
    if (
        !drawing ||
        drawing.coordinate_frame !== 'PRODUCT_CAD_STEP_NATIVE' ||
        drawing.units !== 'mm' ||
        Number(drawing.model_rendering?.model_units_per_mm) !== MODEL_UNITS_PER_MM
    )
    {
        throw Error('지원하지 않는 CAD 좌표/축척 계약입니다.');
    }
}

function updateScene(drawing)
{
    const ptm = drawing.ptm_pose ?? drawing.camera_pose;
    state.viewer.setPose(drawing.pose_frame, drawing.product_rotation_deg, drawing.product_tilt_deg, drawing.product_translation_mm);
    state.viewer.setPtmPose(ptm);
    state.viewer.setAssembly(drawing.assembly_setup || null);
    state.viewer.setSceneOptions({floor: byId('cadFloor').checked, ptm: byId('cadPtm').checked,
        axes: byId('cadAxes').checked, worker: byId('cadOperator').checked});
    const frame = drawing.pose_frame || {};
    const longAxis = 'XYZ'[frame.tilt_axis] || 'X';
    const legacy = frame.convention !== 'operator_clockwise_v1';
    byId('cadSceneStatus').textContent = [
        `${legacy ? '기존 회전' : '좌우 회전'} ${Number(drawing.product_rotation_deg || 0).toFixed(1)}° · ${legacy ? '기존 틸팅' : '긴축 틸팅'} ${Number(drawing.product_tilt_deg || 0).toFixed(1)}°`,
        `청록: ${legacy ? '기존 틸트축' : '제품 긴축'}(+${longAxis}) / 중심 틸트축 · 노랑: 바닥 수직(+Z) · 바닥면: 표시 기준`,
        ptm ? `PTM 레이저 기준점 XYZ(mm): ${ptm.position_mm.map(value => Number(value).toFixed(1)).join(', ')} · ${ptm.source === 'manual_display' ? '수동 표시 위치' : '기준점 각도 보정 추정'}`
            : 'PTM 레이저 모듈: 레시피 기준점 캘리브레이션 후 자동 표시',
        ptm ? '빨강: 선택 포인트 방향의 모의 조사선 · 실제 레이저 ON 표시 아님' : '',
        frame.convention === 'operator_clockwise_v1' ? '+ 회전: 위에서 시계방향 · + 틸팅: 제품 전면이 작업자쪽' : '',
        ptm?.source === 'manual_display' ? 'PTM 위치: 수동 표시 · 실제 보정계산은 기준점 캘리브레이션 사용' : '',
        drawing.scene_preview ? '작업자·PTM 표시 미리보기 (미저장)' : '',
        legacy ? '이전 축으로 저장된 자세: 기존 이동값 유지 중 · 새 축 적용 시 자세 보정을 다시 적용하세요.' : '',
    ].filter(Boolean).join('\n');
}

async function loadDrawing(drawing)
{
    validateDrawing(drawing);
    state.drawing = drawing;
    state.points = viewerPoints(drawing.points);
    const modelUrl = drawing.model_url;

    if (!state.viewer || state.modelUrl !== modelUrl)
    {
        state.viewer?.destroy();
        state.viewer = await createStandaloneViewer({
            canvas: byId('cadCanvas'),
            pointLayer: byId('cadPointLayer'),
            ptmLabel: byId('cadPtmLabel'),
            operatorLabel: byId('cadOperatorLabel'),
            modelUrl,
            points: state.points,
            onSelect: point => showPoint(point)
        });
        state.modelUrl = modelUrl;
    }
    else
    {
        state.viewer.setPoints(state.points);
    }

    if (state.selectedPointId)
    {
        const selected = state.points.find(point => point.id === state.selectedPointId);
        if (selected)
        {
            state.viewer.selectPoint(selected.group_id, selected.target_id);
        }
    }
    updateScene(drawing);
    state.viewer.render();
}

window.addEventListener('drawing-cad-load', event => {
    const stage = byId('cadStage');
    stage.setAttribute('aria-busy', 'true');
    const ready = loadDrawing(event.detail?.drawing)
        .catch(error => {
            byId('drawingStatus').textContent = `CAD 로딩 실패: ${error.message}`;
            throw error;
        })
        .finally(() => stage.removeAttribute('aria-busy'));
    if (event.detail) event.detail.ready = ready;
    ready.catch(() => {});
});

window.addEventListener('drawing-cad-points', event => {
    const drawing = event.detail?.drawing;
    if (!drawing || !state.viewer)
    {
        return;
    }
    state.drawing = drawing;
    state.points = viewerPoints(drawing.points);
    state.viewer.setPoints(state.points);
    updateScene(drawing);
});

for (const id of ['cadFloor', 'cadPtm', 'cadAxes', 'cadOperator'])
{
    byId(id).addEventListener('change', () => {
        if (state.viewer && state.drawing) updateScene(state.drawing);
    });
}

window.addEventListener('drawing-cad-select', event => {
    const point = state.points.find(item => item.id === event.detail?.point_id);
    if (!point || !state.viewer)
    {
        return;
    }
    state.selectedPointId = point.id;
    state.viewer.selectPoint(point.group_id, point.target_id);
    showPoint(point, false);
});

window.addEventListener('drawing-tab-visible', () => {
    requestAnimationFrame(() => state.viewer?.render());
});

document.querySelectorAll('[data-cad-view]').forEach(button => {
    button.addEventListener('click', () => state.viewer?.setPreset(button.dataset.cadView));
});

for (const [ids, className] of [
    [['cadLabels', 'drawingPointLabels'], 'show-labels'],
    [['cadTargetLabels', 'drawingTargetLabels'], 'show-target-labels']
])
{
    for (const id of ids)
    {
        byId(id)?.addEventListener('change', event => {
            const visible = event.target.checked;
            for (const otherId of ids)
            {
                if (byId(otherId)) byId(otherId).checked = visible;
            }
            byId('cadStage').classList.toggle(className, visible);
        });
    }
}

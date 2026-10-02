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
    state.viewer.render();
}

window.addEventListener('drawing-cad-load', event => {
    loadDrawing(event.detail?.drawing).catch(error => {
        byId('drawingStatus').textContent = `CAD 로딩 실패: ${error.message}`;
    });
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
});

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

byId('cadLabels')?.addEventListener('change', event => {
    byId('cadStage').classList.toggle('show-labels', event.target.checked);
});

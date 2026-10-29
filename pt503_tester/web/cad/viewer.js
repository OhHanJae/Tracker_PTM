import * as THREE from './vendor/three.module.js';
import {OrbitControls} from './vendor/controls/OrbitControls.js';
import {RoomEnvironment} from './vendor/environments/RoomEnvironment.js';
import {loadGlb} from './glb_loader.js';

export const MODEL_UNITS_PER_MM = 0.1;

const BACKGROUND_COLOR = 0x20252a;
const DEFAULT_DIRECTION = new THREE.Vector3(1, -1, 0.75).normalize();
const PRESETS = {
    top: [0, 0, 1],
    bottom: [0, 0, -1],
    front: [0, -1, 0],
    back: [0, 1, 0],
    left: [1, 0, 0],
    right: [-1, 0, 0]
};

export function makeProductPoseMatrix(frame, rotation = 0, tilt = 0, translation = [0, 0, 0])
{
    const matrix = new THREE.Matrix4();
    if (frame)
    {
        const pivot = new THREE.Vector3(...frame.center).multiplyScalar(MODEL_UNITS_PER_MM);
        const axis = index => new THREE.Vector3(...[0, 1, 2].map(value => value === index ? 1 : 0));
        const yaw = new THREE.Matrix4().makeRotationAxis(axis(frame.rotation_axis), THREE.MathUtils.degToRad((rotation + (frame.zero_rotation_deg || 0)) * (frame.rotation_sign ?? 1)));
        const pitch = new THREE.Matrix4().makeRotationAxis(axis(frame.tilt_axis), THREE.MathUtils.degToRad((tilt + (frame.zero_tilt_deg || 0)) * (frame.tilt_sign ?? 1)));
        matrix.makeTranslation(pivot.x, pivot.y, pivot.z);
        if (['floor_yaw_long_tilt_v1', 'operator_clockwise_v1'].includes(frame.convention)) matrix.multiply(yaw).multiply(pitch);
        else matrix.multiply(pitch).multiply(yaw);
        matrix.multiply(new THREE.Matrix4().makeTranslation(-pivot.x, -pivot.y, -pivot.z));
    }
    const offset = new THREE.Vector3(...translation).multiplyScalar(MODEL_UNITS_PER_MM);
    return matrix.premultiply(new THREE.Matrix4().makeTranslation(offset.x, offset.y, offset.z));
}

function modelStats(root)
{
    const stats = {meshCount: 0, vertexCount: 0, triangleCount: 0};
    root.traverse(object => {
        if (!object.isMesh || !object.geometry)
        {
            return;
        }
        const geometry = object.geometry;
        const position = geometry.getAttribute('position');
        const indexCount = geometry.index?.count || 0;
        stats.meshCount += 1;
        stats.vertexCount += position?.count || 0;
        stats.triangleCount += Math.floor((indexCount || position?.count || 0) / 3);
    });
    return stats;
}

function prepareModel(root)
{
    root.traverse(object => {
        if (!object.isMesh || !object.geometry)
        {
            return;
        }
        if (!object.geometry.getAttribute('normal'))
        {
            object.geometry.computeVertexNormals();
        }
        const materials = Array.isArray(object.material) ? object.material : [object.material];
        for (const material of materials)
        {
            if (!material)
            {
                continue;
            }
            if ('envMapIntensity' in material)
            {
                material.envMapIntensity = 1.05;
            }
            material.needsUpdate = true;
        }
    });
}

function disposeObject(root)
{
    const geometries = new Set();
    const materials = new Set();
    const textures = new Set();
    root.traverse(object => {
        if (object.geometry)
        {
            geometries.add(object.geometry);
        }
        const objectMaterials = Array.isArray(object.material) ? object.material : [object.material];
        for (const material of objectMaterials)
        {
            if (!material)
            {
                continue;
            }
            materials.add(material);
            for (const value of Object.values(material))
            {
                if (value?.isTexture)
                {
                    textures.add(value);
                }
            }
        }
    });
    textures.forEach(texture => texture.dispose());
    materials.forEach(material => material.dispose());
    geometries.forEach(geometry => geometry.dispose());
}

export async function createStandaloneViewer({
    canvas,
    pointLayer,
    ptmLabel = null,
    operatorLabel = null,
    modelUrl,
    points = [],
    onSelect = null
})
{
    const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: false,
        powerPreference: 'high-performance'
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.92;
    renderer.setClearColor(BACKGROUND_COLOR, 1);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(BACKGROUND_COLOR);

    const pmrem = new THREE.PMREMGenerator(renderer);
    const room = new RoomEnvironment();
    const environmentTarget = pmrem.fromScene(room);
    scene.environment = environmentTarget.texture;
    scene.environmentIntensity = 0.58;
    disposeObject(room);

    const hemisphere = new THREE.HemisphereLight(0xe8f1ff, 0x303840, 0.72);
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.65);
    keyLight.position.set(4, -5, 7);
    const fillLight = new THREE.DirectionalLight(0x9db8d0, 0.32);
    fillLight.position.set(-5, 3, 2);
    scene.add(hemisphere, keyLight, fillLight);

    const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100000);
    camera.up.set(0, 0, 1);

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.075;
    controls.rotateSpeed = 0.55;
    controls.zoomSpeed = 0.72;
    controls.panSpeed = 0.65;
    controls.screenSpacePanning = true;
    controls.zoomToCursor = true;
    controls.minPolarAngle = 0.001;
    controls.maxPolarAngle = Math.PI - 0.001;
    controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
    controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN;
    controls.mouseButtons.RIGHT = THREE.MOUSE.PAN;

    const gltf = await loadGlb(modelUrl);
    const model = gltf.scene;
    prepareModel(model);
    const poseRoot = new THREE.Group();
    poseRoot.matrixAutoUpdate = false;
    poseRoot.add(model);
    scene.add(poseRoot);
    const poseMatrix = new THREE.Matrix4();

    const bounds = new THREE.Box3().setFromObject(model);
    if (bounds.isEmpty())
    {
        throw new Error('GLB scene does not contain visible geometry.');
    }
    const center = bounds.getCenter(new THREE.Vector3());
    const size = bounds.getSize(new THREE.Vector3());
    const radius = Math.max(size.length() * 0.5, 0.001);
    const stats = modelStats(model);

    const floor = new THREE.Group();
    floor.position.set(center.x, center.y, bounds.min.z - radius * 0.35);
    floor.add(new THREE.Mesh(new THREE.PlaneGeometry(radius * 5, radius * 5),
        new THREE.MeshBasicMaterial({color: 0x506274, transparent: true, opacity: 0.2,
            side: THREE.DoubleSide, depthWrite: false})));
    const grid = new THREE.GridHelper(radius * 5, 20, 0x7692a9, 0x415566);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = radius * 0.001;
    floor.add(grid);
    scene.add(floor);

    const directions = new THREE.Group();
    poseRoot.add(directions);
    const verticalAxis = new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), center,
        radius * 0.5, 0xf5ca64, radius * 0.07, radius * 0.035);
    scene.add(verticalAxis);
    const mockPtm = new THREE.Group();
    scene.add(mockPtm);
    let ptmPose = null;
    let ptmPoseKey = '';
    let ptmHead = null;
    let laserLine = null;
    let laserTarget = null;
    let ptmUnit = 0;
    const worker = new THREE.Group();
    scene.add(worker);
    let assembly = null;
    let assemblyKey = '';
    let workerHeight = 170;
    let workerEye = new THREE.Vector3();
    let poseKey = '';
    let axesKey = '';
    const sceneOptions = {floor: true, ptm: true, axes: true, worker: true};

    function setAssembly(value, {fit = true} = {})
    {
        const key = JSON.stringify(value || null);
        if (key === assemblyKey) return;
        assemblyKey = key;
        assembly = value;
        disposeObject(worker);
        worker.clear();
        worker.visible = Boolean(value) && sceneOptions.worker;
        if (value)
        {
            workerHeight = Number(value.operator_height_mm) * MODEL_UNITS_PER_MM;
            worker.position.fromArray(value.operator_position_mm).multiplyScalar(MODEL_UNITS_PER_MM);
            const target = new THREE.Vector3(...value.product_origin_mm).multiplyScalar(MODEL_UNITS_PER_MM);
            const direction = target.clone().sub(worker.position);
            worker.rotation.z = Math.atan2(-direction.x, direction.y);
            const clothing = new THREE.MeshStandardMaterial({color: 0x68b88f, roughness: 0.8});
            const skin = new THREE.MeshStandardMaterial({color: 0xe2b18b, roughness: 0.85});
            const headPivot = new THREE.Group();
            headPivot.position.z = workerHeight * 0.91;
            const head = new THREE.Mesh(new THREE.SphereGeometry(workerHeight * 0.075, 18, 12), skin);
            const body = new THREE.Mesh(new THREE.CylinderGeometry(workerHeight * 0.11, workerHeight * 0.095,
                workerHeight * 0.32, 14), clothing);
            body.rotation.x = Math.PI / 2;
            body.position.z = workerHeight * 0.65;
            worker.add(headPivot, body);
            const nose = new THREE.Mesh(new THREE.ConeGeometry(workerHeight * 0.025, workerHeight * 0.06, 10), skin);
            nose.position.y = workerHeight * 0.078;
            headPivot.add(head, nose);
            const localTarget = target.clone().sub(worker.position).applyQuaternion(worker.quaternion.clone().invert());
            const headDirection = localTarget.clone().sub(headPivot.position);
            headPivot.rotation.x = Math.atan2(headDirection.z, Math.hypot(headDirection.x, headDirection.y));
            for (const side of [-1, 1])
            {
                const leg = new THREE.Mesh(new THREE.CylinderGeometry(workerHeight * 0.035, workerHeight * 0.03,
                    workerHeight * 0.44, 10), clothing);
                leg.rotation.x = Math.PI / 2;
                leg.position.set(side * workerHeight * 0.055, 0, workerHeight * 0.25);
                const arm = new THREE.Mesh(new THREE.CylinderGeometry(workerHeight * 0.025, workerHeight * 0.025,
                    workerHeight * 0.30, 10), clothing);
                arm.rotation.x = Math.PI / 2;
                arm.position.set(side * workerHeight * 0.145, 0, workerHeight * 0.64);
                worker.add(leg, arm);
            }
            workerEye.copy(worker.position).add(new THREE.Vector3(0, 0, workerHeight * 0.94));
            const guideOrigin = new THREE.Vector3(0, workerHeight * 0.1, workerHeight * 0.72);
            const guide = new THREE.ArrowHelper(localTarget.clone().sub(guideOrigin).normalize(),
                guideOrigin, radius * 0.35,
                0x68df99, radius * 0.05, radius * 0.025);
            worker.add(guide);
        }
        if (fit) fitModel(camera.position.clone().sub(controls.target));
        render();
    }

    function updateDirections(frame, translation)
    {
        const longAxis = frame?.tilt_axis ?? (size.x >= size.y ? 0 : 1);
        const pivot = frame ? new THREE.Vector3(...frame.center).multiplyScalar(MODEL_UNITS_PER_MM) : center;
        verticalAxis.position.copy(pivot).add(new THREE.Vector3(...translation).multiplyScalar(MODEL_UNITS_PER_MM));
        const key = JSON.stringify([pivot.toArray(), longAxis]);
        if (key === axesKey) return;
        axesKey = key;
        disposeObject(directions);
        directions.clear();
        const axis = new THREE.Vector3(...[0, 1, 2].map(index => index === longAxis ? 1 : 0));
        directions.add(new THREE.ArrowHelper(axis, pivot, radius * 0.65, 0x42d9cd,
            radius * 0.07, radius * 0.035));
        const other = new THREE.Vector3(longAxis === 0 ? 0 : 1, longAxis === 0 ? 1 : 0, 0);
        directions.add(new THREE.ArrowHelper(other, pivot, radius * 0.4, 0xf29265,
            radius * 0.06, radius * 0.03));
        const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([
            pivot.clone().addScaledVector(axis, -radius * 0.55), pivot.clone().addScaledVector(axis, radius * 0.55)]),
            new THREE.LineDashedMaterial({color: 0x42d9cd, dashSize: radius * 0.03, gapSize: radius * 0.02}));
        line.computeLineDistances();
        directions.add(line);
        const marker = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.014, 12, 8),
            new THREE.MeshBasicMaterial({color: 0xffffff}));
        marker.position.copy(pivot);
        directions.add(marker);
    }

    function setPtmPose(value, {fit = true} = {})
    {
        const valid = value && ['position_mm', 'right', 'up', 'forward'].every(key =>
            Array.isArray(value[key]) && value[key].length === 3 && value[key].every(Number.isFinite));
        const key = JSON.stringify(valid ? value : null);
        if (key === ptmPoseKey) return;
        ptmPoseKey = key;
        ptmPose = valid ? value : null;
        disposeObject(mockPtm);
        mockPtm.clear();
        ptmHead = laserLine = laserTarget = null;
        mockPtm.visible = Boolean(ptmPose) && sceneOptions.ptm;
        if (ptmPose)
        {
            const unit = radius * 0.065;
            ptmUnit = unit;
            const housing = new THREE.MeshStandardMaterial({color: 0x7da1b7, metalness: 0.3, roughness: 0.5});
            const dark = new THREE.MeshStandardMaterial({color: 0x334959, roughness: 0.65});
            const base = new THREE.Mesh(new THREE.CylinderGeometry(unit, unit * 1.1, unit * 0.4, 24), dark);
            base.rotation.x = Math.PI / 2;
            base.position.z = -unit * 1.8;
            const pan = new THREE.Mesh(new THREE.CylinderGeometry(unit * 0.45, unit * 0.6, unit * 0.8, 20), housing);
            pan.rotation.x = Math.PI / 2;
            pan.position.z = -unit * 1.25;
            mockPtm.add(base, pan);
            for (const side of [-1, 1])
            {
                const arm = new THREE.Mesh(new THREE.BoxGeometry(unit * 0.25, unit * 0.5, unit * 1.5), housing);
                arm.position.set(side * unit * 0.72, 0, -unit * 0.45);
                mockPtm.add(arm);
            }
            ptmHead = new THREE.Group();
            const head = new THREE.Mesh(new THREE.CylinderGeometry(unit * 0.52, unit * 0.52, unit * 1.4, 20), housing);
            head.rotation.x = Math.PI / 2;
            const aperture = new THREE.Mesh(new THREE.CylinderGeometry(unit * 0.35, unit * 0.35, unit * 0.15, 20), dark);
            aperture.rotation.x = Math.PI / 2;
            aperture.position.z = unit * 0.76;
            const emitter = new THREE.Mesh(new THREE.CircleGeometry(unit * 0.18, 20),
                new THREE.MeshBasicMaterial({color: 0xff453c, side: THREE.DoubleSide}));
            emitter.position.z = unit * 0.85;
            ptmHead.add(head, aperture, emitter);
            laserLine = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
                new THREE.LineBasicMaterial({color: 0xff5145, transparent: true, opacity: 0.85}));
            laserTarget = new THREE.Mesh(new THREE.SphereGeometry(unit * 0.08, 10, 8),
                new THREE.MeshBasicMaterial({color: 0xff5145}));
            mockPtm.add(ptmHead, laserLine, laserTarget);
            mockPtm.position.fromArray(ptmPose.position_mm).multiplyScalar(MODEL_UNITS_PER_MM);
        }
        if (fit) fitModel(camera.position.clone().sub(controls.target));
        render();
    }

    function updatePtmDirection()
    {
        if (!ptmHead) return;
        const point = currentPoints.find(item => pointKey(item) === selectedKey);
        const target = point ? new THREE.Vector3(...point.xyz_mm).multiplyScalar(MODEL_UNITS_PER_MM) : center.clone();
        target.applyMatrix4(poseMatrix).sub(mockPtm.position);
        const direction = target.clone().normalize();
        if (direction.lengthSq() < 1e-12) return;
        ptmHead.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), direction);
        const positions = laserLine.geometry.getAttribute('position');
        positions.setXYZ(0, direction.x * ptmUnit * 0.9, direction.y * ptmUnit * 0.9, direction.z * ptmUnit * 0.9);
        positions.setXYZ(1, target.x, target.y, target.z);
        positions.needsUpdate = true;
        laserLine.geometry.computeBoundingSphere();
        laserTarget.position.copy(target);
    }

    let currentPoints = Array.isArray(points) ? points : [];
    let selectedKey = null;
    let markers = [];
    let destroyed = false;
    let animationFrame = 0;
    let lastWidth = 0;
    let lastHeight = 0;
    let pointPress = null;
    const pointSurface = canvas.parentElement;

    function fitModel(direction = DEFAULT_DIRECTION)
    {
        if (direction.lengthSq() < 1e-12) direction = DEFAULT_DIRECTION;
        const viewBounds = bounds.clone().applyMatrix4(poseMatrix);
        if (ptmPose && sceneOptions.ptm) viewBounds.expandByPoint(mockPtm.position);
        if (assembly && sceneOptions.worker) viewBounds.expandByPoint(worker.position).expandByPoint(workerEye);
        const viewCenter = viewBounds.getCenter(new THREE.Vector3());
        const viewRadius = Math.max(viewBounds.getSize(new THREE.Vector3()).length() * 0.5, radius);
        const rect = canvas.getBoundingClientRect();
        const aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1);
        const verticalFov = THREE.MathUtils.degToRad(camera.fov);
        let distance = viewRadius / Math.sin(verticalFov * 0.5);
        if (aspect < 1)
        {
            distance /= aspect;
        }
        distance *= 1.12;
        camera.position.copy(viewCenter).addScaledVector(direction.clone().normalize(), distance);
        camera.near = Math.max(viewRadius / 1000, 0.001);
        camera.far = Math.max(distance + viewRadius * 40, camera.near * 100);
        camera.updateProjectionMatrix();
        controls.target.copy(viewCenter);
        controls.minDistance = Math.max(radius * 0.015, 0.001);
        controls.maxDistance = viewRadius * 60;
        controls.update();
    }

    function resize()
    {
        const rect = canvas.getBoundingClientRect();
        const width = Math.max(1, Math.round(rect.width));
        const height = Math.max(1, Math.round(rect.height));
        if (width === lastWidth && height === lastHeight)
        {
            return;
        }
        lastWidth = width;
        lastHeight = height;
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
    }

    function pointKey(point)
    {
        return `${point.group_id}:${point.target_id}`;
    }

    function rebuildPointMarkers()
    {
        pointLayer.replaceChildren();
        markers = currentPoints.map(point => {
            const marker = document.createElement('button');
            marker.type = 'button';
            marker.className = `cad-point group-${point.group_id}`;
            if (point.calibration)
            {
                marker.classList.add('calibration-point');
            }
            marker.dataset.label = point.order ?? point.id;
            marker.dataset.targetLabel = point.target_id;
            marker.title = `포인트 ID: ${marker.dataset.label}\n타겟 ID: ${point.target_id}`;
            marker.addEventListener('click', event => {
                event.stopPropagation();
                if (event.detail === 0) select(point);
            });
            pointLayer.appendChild(marker);
            return {point, marker, world: new THREE.Vector3()};
        });
    }

    function updatePointMarkers()
    {
        const rect = canvas.getBoundingClientRect();
        if (ptmLabel)
        {
            const projected = mockPtm.position.clone().project(camera);
            ptmLabel.hidden = !mockPtm.visible || projected.z < -1 || projected.z > 1 ||
                Math.abs(projected.x) > 1 || Math.abs(projected.y) > 1;
            ptmLabel.style.left = `${(projected.x * 0.5 + 0.5) * rect.width}px`;
            ptmLabel.style.top = `${(0.5 - projected.y * 0.5) * rect.height}px`;
        }
        if (operatorLabel)
        {
            const projected = workerEye.clone().project(camera);
            operatorLabel.hidden = !worker.visible || projected.z < -1 || projected.z > 1 ||
                Math.abs(projected.x) > 1 || Math.abs(projected.y) > 1;
            operatorLabel.style.left = `${(projected.x * 0.5 + 0.5) * rect.width}px`;
            operatorLabel.style.top = `${(0.5 - projected.y * 0.5) * rect.height}px`;
        }
        for (const item of markers)
        {
            const xyz = item.point.xyz_mm || [];
            item.world.set(
                Number(xyz[0] || 0) * MODEL_UNITS_PER_MM,
                Number(xyz[1] || 0) * MODEL_UNITS_PER_MM,
                Number(xyz[2] || 0) * MODEL_UNITS_PER_MM
            ).applyMatrix4(poseMatrix).project(camera);
            const visible = item.world.z >= -1 && item.world.z <= 1 &&
                item.world.x >= -1.05 && item.world.x <= 1.05 &&
                item.world.y >= -1.05 && item.world.y <= 1.05;
            item.marker.hidden = !visible;
            if (!visible)
            {
                continue;
            }
            item.marker.style.left = `${(item.world.x * 0.5 + 0.5) * rect.width}px`;
            item.marker.style.top = `${(1 - (item.world.y * 0.5 + 0.5)) * rect.height}px`;
            item.marker.classList.toggle('selected', pointKey(item.point) === selectedKey);
        }
    }

    function draw()
    {
        animationFrame = 0;
        if (destroyed)
        {
            return;
        }
        resize();
        const moving = controls.update();
        worker.visible = Boolean(assembly) && sceneOptions.worker && camera.position.distanceTo(workerEye) > workerHeight * 0.15;
        updatePtmDirection();
        renderer.render(scene, camera);
        updatePointMarkers();
        if (moving)
        {
            requestRender();
        }
    }

    function requestRender()
    {
        if (!animationFrame && !destroyed)
        {
            animationFrame = window.requestAnimationFrame(draw);
        }
    }

    function render()
    {
        requestRender();
    }

    function select(point)
    {
        selectedKey = point ? pointKey(point) : null;
        for (const item of markers)
        {
            item.marker.classList.toggle('selected', pointKey(item.point) === selectedKey);
        }
        onSelect?.(point || null);
        render();
    }

    function pointAtPointer(event)
    {
        let nearest = null;
        let distance = event.pointerType === 'touch' ? 22 : 14;
        for (const item of markers)
        {
            if (item.marker.hidden) continue;
            const rect = item.marker.getBoundingClientRect();
            const next = Math.hypot(event.clientX - rect.left - rect.width / 2,
                event.clientY - rect.top - rect.height / 2);
            if (next < distance)
            {
                nearest = item.point;
                distance = next;
            }
        }
        return nearest;
    }

    function pointPointerDown(event)
    {
        pointPress = null;
        if (event.button !== 0 || event.isPrimary === false) return;
        pointPress = {id: event.pointerId, x: event.clientX, y: event.clientY,
            point: pointAtPointer(event), moved: false};
        if (pointPress.point) event.preventDefault();
    }

    function pointPointerMove(event)
    {
        if (pointPress?.id === event.pointerId &&
            Math.hypot(event.clientX - pointPress.x, event.clientY - pointPress.y) > 6)
        {
            pointPress.moved = true;
        }
    }

    function pointPointerUp(event)
    {
        const press = pointPress;
        pointPress = null;
        if (!press || press.id !== event.pointerId || press.moved ||
            Math.hypot(event.clientX - press.x, event.clientY - press.y) > 6) return;
        const point = press.point || pointAtPointer(event);
        if (point && currentPoints.includes(point)) select(point);
    }

    function pointPointerCancel()
    {
        pointPress = null;
    }

    pointSurface.addEventListener('pointerdown', pointPointerDown, true);
    pointSurface.addEventListener('pointermove', pointPointerMove, true);
    pointSurface.addEventListener('pointerup', pointPointerUp, true);
    pointSurface.addEventListener('pointercancel', pointPointerCancel, true);
    controls.addEventListener('change', requestRender);
    const resizeObserver = new ResizeObserver(requestRender);
    resizeObserver.observe(canvas);
    canvas.addEventListener('contextmenu', event => event.preventDefault());

    updateDirections(null, [0, 0, 0]);
    rebuildPointMarkers();
    fitModel();
    draw();

    return {
        setPose(frame, rotation = 0, tilt = 0, translation = [0, 0, 0], {fit = true} = {})
        {
            poseMatrix.copy(makeProductPoseMatrix(frame, rotation, tilt, translation));
            poseRoot.matrix.copy(poseMatrix);
            poseRoot.matrixWorldNeedsUpdate = true;
            updateDirections(frame, translation);
            const key = JSON.stringify([frame, rotation, tilt, translation]);
            if (fit && key !== poseKey) fitModel(camera.position.clone().sub(controls.target));
            poseKey = key;
            render();
        },
        setPtmPose,
        setAssembly,
        setSceneOptions(value)
        {
            Object.assign(sceneOptions, value);
            floor.visible = sceneOptions.floor;
            directions.visible = verticalAxis.visible = sceneOptions.axes;
            mockPtm.visible = Boolean(ptmPose) && sceneOptions.ptm;
            worker.visible = Boolean(assembly) && sceneOptions.worker;
            render();
        },
        setPoints(value)
        {
            currentPoints = Array.isArray(value) ? value : [];
            selectedKey = null;
            rebuildPointMarkers();
            render();
        },
        selectPoint(groupId, targetId)
        {
            const point = currentPoints.find(item =>
                Number(item.group_id) === Number(groupId) &&
                String(item.target_id) === String(targetId)
            );
            select(point || null);
        },
        setPreset(name)
        {
            if (name === 'operator' && assembly)
            {
                camera.position.copy(workerEye);
                controls.target.fromArray(assembly.product_origin_mm).multiplyScalar(MODEL_UNITS_PER_MM);
                controls.update();
            }
            else if (name === 'reset')
            {
                fitModel();
            }
            else if (PRESETS[name])
            {
                fitModel(new THREE.Vector3(...PRESETS[name]));
            }
            render();
        },
        destroy()
        {
            destroyed = true;
            if (animationFrame)
            {
                window.cancelAnimationFrame(animationFrame);
            }
            resizeObserver.disconnect();
            pointSurface.removeEventListener('pointerdown', pointPointerDown, true);
            pointSurface.removeEventListener('pointermove', pointPointerMove, true);
            pointSurface.removeEventListener('pointerup', pointPointerUp, true);
            pointSurface.removeEventListener('pointercancel', pointPointerCancel, true);
            controls.removeEventListener('change', requestRender);
            controls.dispose();
            pointLayer.replaceChildren();
            if (ptmLabel) ptmLabel.hidden = true;
            if (operatorLabel) operatorLabel.hidden = true;
            scene.remove(poseRoot);
            for (const object of [poseRoot, floor, verticalAxis, mockPtm, worker])
            {
                scene.remove(object);
                disposeObject(object);
            }
            environmentTarget.dispose();
            pmrem.dispose();
            renderer.dispose();
        },
        render,
        stats
    };
}

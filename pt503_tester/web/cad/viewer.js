import * as THREE from './vendor/three.module.js';
import {OrbitControls} from './vendor/controls/OrbitControls.js';
import {RoomEnvironment} from './vendor/environments/RoomEnvironment.js';
import {loadGlb} from './glb_loader.js';

export const MODEL_UNITS_PER_MM = 0.1;

const BACKGROUND_COLOR = 0x20252a;
const DEFAULT_DIRECTION = new THREE.Vector3(1, -1, 0.75).normalize();
const PRESETS = {
    top: [0, 0, -1],
    bottom: [0, 0, 1],
    front: [0, -1, 0],
    back: [0, 1, 0],
    left: [1, 0, 0],
    right: [-1, 0, 0]
};

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
    scene.add(model);

    const bounds = new THREE.Box3().setFromObject(model);
    if (bounds.isEmpty())
    {
        throw new Error('GLB scene does not contain visible geometry.');
    }
    const center = bounds.getCenter(new THREE.Vector3());
    const size = bounds.getSize(new THREE.Vector3());
    const radius = Math.max(size.length() * 0.5, 0.001);
    const stats = modelStats(model);

    let currentPoints = Array.isArray(points) ? points : [];
    let selectedKey = null;
    let markers = [];
    let destroyed = false;
    let animationFrame = 0;
    let lastWidth = 0;
    let lastHeight = 0;

    function fitModel(direction = DEFAULT_DIRECTION)
    {
        const rect = canvas.getBoundingClientRect();
        const aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1);
        const verticalFov = THREE.MathUtils.degToRad(camera.fov);
        let distance = radius / Math.sin(verticalFov * 0.5);
        if (aspect < 1)
        {
            distance /= aspect;
        }
        distance *= 1.12;
        camera.position.copy(center).addScaledVector(direction.clone().normalize(), distance);
        camera.near = Math.max(radius / 1000, 0.001);
        camera.far = Math.max(distance + radius * 40, camera.near * 100);
        camera.updateProjectionMatrix();
        controls.target.copy(center);
        controls.minDistance = Math.max(radius * 0.015, 0.001);
        controls.maxDistance = radius * 60;
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
            marker.dataset.label = point.target_id;
            marker.title = point.target_id;
            marker.addEventListener('click', event => {
                event.stopPropagation();
                select(point);
            });
            pointLayer.appendChild(marker);
            return {point, marker, world: new THREE.Vector3()};
        });
    }

    function updatePointMarkers()
    {
        const rect = canvas.getBoundingClientRect();
        for (const item of markers)
        {
            const xyz = item.point.xyz_mm || [];
            item.world.set(
                Number(xyz[0] || 0) * MODEL_UNITS_PER_MM,
                Number(xyz[1] || 0) * MODEL_UNITS_PER_MM,
                Number(xyz[2] || 0) * MODEL_UNITS_PER_MM
            ).project(camera);
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

    controls.addEventListener('change', requestRender);
    const resizeObserver = new ResizeObserver(requestRender);
    resizeObserver.observe(canvas);
    canvas.addEventListener('contextmenu', event => event.preventDefault());

    rebuildPointMarkers();
    fitModel();
    draw();

    return {
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
            if (name === 'reset')
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
            controls.removeEventListener('change', requestRender);
            controls.dispose();
            pointLayer.replaceChildren();
            scene.remove(model);
            disposeObject(model);
            environmentTarget.dispose();
            pmrem.dispose();
            renderer.dispose();
        },
        render,
        stats
    };
}

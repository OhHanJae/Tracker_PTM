"""3D drawing loading, picking, and pan/tilt calibration helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import QPoint, QPointF, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
    from PySide6.QtWidgets import QWidget
except ImportError:  # pragma: no cover - used by server-only installs.
    QPoint = QPointF = QColor = QFont = QMouseEvent = QPainter = QPen = QPolygonF = QWheelEvent = object  # type: ignore[assignment]

    class _QtFallback:
        class AlignmentFlag:
            AlignCenter = 0

        class MouseButton:
            LeftButton = 1
            RightButton = 2

        class PenStyle:
            NoPen = 0

        class RenderHint:
            Antialiasing = 0

    class _SignalFallback:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def emit(self, *_args: object, **_kwargs: object) -> None:
            pass

    class QWidget:  # type: ignore[no-redef]
        pass

    Qt = _QtFallback()  # type: ignore[assignment]
    Signal = _SignalFallback  # type: ignore[assignment]

Vector3 = tuple[float, float, float]
Face = tuple[int, int, int]
FaceColor = tuple[int, int, int]

MESH_FILE_EXTENSIONS = (".glb", ".gltf", ".obj", ".stl", ".ply")
CACHE_FORMAT_VERSION = 1

STEP_POINT_RE = re.compile(
    r"#\d+\s*=\s*CARTESIAN_POINT\s*\([^,]*,\s*\(([^()]*)\)\)",
    re.IGNORECASE | re.DOTALL,
)
STEP_REF_RE = re.compile(r"#(\d+)")
STEP_BOOL_RE = re.compile(r"\.(T|F)\.", re.IGNORECASE)

COMPONENT_FORMATS: dict[int, tuple[str, int]] = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
ACCESSOR_COMPONENTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


class ModelLoadError(ValueError):
    """Raised when a drawing file cannot be converted into display geometry."""


class CalibrationError(ValueError):
    """Raised when calibration points are insufficient or numerically invalid."""


@dataclass(frozen=True)
class MeshModel:
    source_path: str
    vertices: list[Vector3]
    faces: list[Face]
    warnings: list[str]
    face_colors: list[FaceColor] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.source_path).name

    @property
    def bounds(self) -> tuple[Vector3, Vector3]:
        if not self.vertices:
            zero = (0.0, 0.0, 0.0)
            return zero, zero
        xs = [vertex[0] for vertex in self.vertices]
        ys = [vertex[1] for vertex in self.vertices]
        zs = [vertex[2] for vertex in self.vertices]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


@dataclass
class DrawingPoint:
    id: str
    x: float
    y: float
    z: float
    calibration: bool = False
    calibration_slot: int | None = None
    pan: float | None = None
    tilt: float | None = None
    label: str = ""

    @classmethod
    def create(cls, position: Vector3) -> DrawingPoint:
        return cls(str(uuid.uuid4()), float(position[0]), float(position[1]), float(position[2]))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DrawingPoint:
        return cls(
            id=str(value.get("id") or uuid.uuid4()),
            x=float(value.get("x", 0.0)),
            y=float(value.get("y", 0.0)),
            z=float(value.get("z", 0.0)),
            calibration=bool(value.get("calibration", False)),
            calibration_slot=(
                None
                if value.get("calibration_slot") is None
                else int(value["calibration_slot"])
            ),
            pan=None if value.get("pan") is None else float(value["pan"]),
            tilt=None if value.get("tilt") is None else float(value["tilt"]),
            label=str(value.get("label", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "calibration": self.calibration,
            "calibration_slot": self.calibration_slot,
            "pan": self.pan,
            "tilt": self.tilt,
            "label": self.label,
        }

    @property
    def position(self) -> Vector3:
        return self.x, self.y, self.z

    @property
    def has_pan_tilt(self) -> bool:
        return self.pan is not None and self.tilt is not None


@dataclass(frozen=True)
class CalibrationFit:
    origin: Vector3
    scale: float
    rms_pan_error: float
    rms_tilt_error: float
    points_used: int
    pan_coefficients: tuple[float, float, float, float] | None = None
    tilt_coefficients: tuple[float, float, float, float] | None = None
    coordinate_axes: tuple[int, int] | None = None
    projective_coefficients: tuple[float, ...] | None = None
    pan_origin: float = 0.0
    tilt_origin: float = 0.0
    pan_residual_coefficients: tuple[float, ...] = ()
    tilt_residual_coefficients: tuple[float, ...] = ()

    def predict(self, point: DrawingPoint) -> tuple[float, float]:
        if self.coordinate_axes is not None and self.projective_coefficients is not None:
            return self._predict_projective(point)
        if self.pan_coefficients is None or self.tilt_coefficients is None:
            raise CalibrationError("캘리브레이션 계수가 없습니다.")
        features = _normalized_features(point.position, self.origin, self.scale)
        pan = _dot4(self.pan_coefficients, features) % 360.0
        tilt = max(-60.0, min(60.0, _dot4(self.tilt_coefficients, features)))
        return round(pan, 2), round(tilt, 2)

    def _predict_projective(self, point: DrawingPoint) -> tuple[float, float]:
        pan, tilt, u, v = self._predict_projective_base(point)
        pan_correction, tilt_correction = self._residual_correction(u, v)
        pan = (pan + pan_correction) % 360.0
        tilt = max(-60.0, min(60.0, tilt + tilt_correction))
        return round(pan, 2), round(tilt, 2)

    def _predict_projective_base(
        self, point: DrawingPoint
    ) -> tuple[float, float, float, float]:
        assert self.coordinate_axes is not None
        assert self.projective_coefficients is not None
        coordinates = point.position
        u = (coordinates[self.coordinate_axes[0]] - self.origin[self.coordinate_axes[0]]) / self.scale
        v = (coordinates[self.coordinate_axes[1]] - self.origin[self.coordinate_axes[1]]) / self.scale
        h = self.projective_coefficients
        denominator = h[6] * u + h[7] * v + 1.0
        if abs(denominator) < 1e-9:
            raise CalibrationError("보정 범위 밖의 도면 포인트입니다.")
        image_x = (h[0] * u + h[1] * v + h[2]) / denominator
        image_y = (h[3] * u + h[4] * v + h[5]) / denominator
        direction = _motor_direction_from_image(
            image_x, image_y, self.pan_origin, self.tilt_origin
        )
        pan = math.degrees(math.atan2(direction[0], direction[2])) % 360.0
        tilt = math.degrees(
            math.atan2(direction[1], math.hypot(direction[0], direction[2]))
        )
        return pan, tilt, u, v

    def _residual_correction(self, u: float, v: float) -> tuple[float, float]:
        if (
            not self.pan_residual_coefficients
            or not self.tilt_residual_coefficients
        ):
            return 0.0, 0.0
        basis = (1.0, u, v, u * u, u * v, v * v)
        return (
            sum(
                a * b
                for a, b in zip(self.pan_residual_coefficients, basis, strict=True)
            ),
            sum(
                a * b
                for a, b in zip(self.tilt_residual_coefficients, basis, strict=True)
            ),
        )


@dataclass(frozen=True)
class InverseXYFit:
    pan_origin: float
    tilt_origin: float
    scale: float
    x_coefficients: tuple[float, float, float, float]
    y_coefficients: tuple[float, float, float, float]
    rms_x_error: float
    rms_y_error: float
    points_used: int

    def predict(self, pan: float, tilt: float) -> tuple[float, float]:
        unwrapped_pan = self.pan_origin + ((float(pan) - self.pan_origin + 180.0) % 360.0) - 180.0
        features = (
            (unwrapped_pan - self.pan_origin) / self.scale,
            (float(tilt) - self.tilt_origin) / self.scale,
            0.0,
            1.0,
        )
        x = _dot4(self.x_coefficients, features)
        y = _dot4(self.y_coefficients, features)
        return round(x, 3), round(y, 3)


class DrawingWorkflowStore:
    """Persist the last drawing workflow separately from hardware presets."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
            path = base / "OSRND" / "PT503Tester" / "drawing_workflow.json"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> tuple[str, list[DrawingPoint]]:
        if not self.path.exists():
            return "", []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "", []
        if not isinstance(data, dict):
            return "", []
        points_data = data.get("points", [])
        points = [
            DrawingPoint.from_dict(item)
            for item in points_data
            if isinstance(item, dict)
        ]
        return str(data.get("model_path", "")), points

    def save(self, model_path: str, points: list[DrawingPoint]) -> None:
        temporary = self.path.with_suffix(".tmp")
        payload = {
            "model_path": model_path,
            "points": [point.to_dict() for point in points],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def load_model_file(path: str | Path, *, fast_step: bool = False) -> MeshModel:
    model_path = Path(path)
    suffix = model_path.suffix.lower()
    if suffix in {".stp", ".step", ".cad"}:
        model = _load_step_model(model_path, fast_preview=fast_step)
    elif suffix == ".obj":
        model = _load_obj(model_path)
    elif suffix == ".stl":
        model = _load_stl(model_path)
    elif suffix == ".ply":
        model = _load_ply(model_path)
    elif suffix == ".glb":
        model = _load_glb(model_path)
    elif suffix == ".gltf":
        model = _load_gltf(model_path)
    elif suffix == ".gl2":
        model = _load_gl2(model_path)
    else:
        raise ModelLoadError(f"지원하지 않는 도면 형식입니다: {suffix or '(확장자 없음)'}")
    if not model.vertices:
        raise ModelLoadError("표시할 수 있는 3D 좌표를 찾지 못했습니다.")
    return model


def fit_affine_calibration(points: list[DrawingPoint]) -> CalibrationFit:
    taught = [point for point in points if point.calibration and point.has_pan_tilt]
    if len(taught) < 4:
        raise CalibrationError("Pan/Tilt가 지정된 캘리브레이션 포인트가 최소 4개 필요합니다.")

    origin, scale = _calibration_origin_scale(taught)
    planar_axes = _planar_coordinate_axes(taught)
    if planar_axes is not None:
        return _fit_planar_projective_calibration(taught, origin, scale, planar_axes)

    matrix = [_normalized_features(point.position, origin, scale) for point in taught]
    pan_values = _unwrap_angles([float(point.pan) for point in taught if point.pan is not None])
    tilt_values = [float(point.tilt) for point in taught if point.tilt is not None]

    pan_coefficients = _ridge_least_squares(matrix, pan_values)
    tilt_coefficients = _ridge_least_squares(matrix, tilt_values)
    pan_errors = [
        _angle_error(_dot4(pan_coefficients, features), target)
        for features, target in zip(matrix, pan_values, strict=False)
    ]
    tilt_errors = [
        _dot4(tilt_coefficients, features) - target
        for features, target in zip(matrix, tilt_values, strict=False)
    ]
    return CalibrationFit(
        origin=origin,
        scale=scale,
        rms_pan_error=_rms(pan_errors),
        rms_tilt_error=_rms(tilt_errors),
        points_used=len(taught),
        pan_coefficients=pan_coefficients,
        tilt_coefficients=tilt_coefficients,
    )


def _fit_planar_projective_calibration(
    points: list[DrawingPoint],
    origin: Vector3,
    scale: float,
    coordinate_axes: tuple[int, int],
) -> CalibrationFit:
    pan_values = _unwrap_angles([float(point.pan) for point in points if point.pan is not None])
    tilt_values = [float(point.tilt) for point in points if point.tilt is not None]
    pan_origin = sum(pan_values) / len(pan_values)
    tilt_origin = sum(tilt_values) / len(tilt_values)
    rows: list[list[float]] = []
    values: list[float] = []
    for point, pan, tilt in zip(points, pan_values, tilt_values, strict=True):
        u = (point.position[coordinate_axes[0]] - origin[coordinate_axes[0]]) / scale
        v = (point.position[coordinate_axes[1]] - origin[coordinate_axes[1]]) / scale
        image_x, image_y = _motor_image_coordinates(pan, tilt, pan_origin, tilt_origin)
        rows.append([u, v, 1.0, 0.0, 0.0, 0.0, -image_x * u, -image_x * v])
        values.append(image_x)
        rows.append([0.0, 0.0, 0.0, u, v, 1.0, -image_y * u, -image_y * v])
        values.append(image_y)

    coefficients = tuple(_least_squares(rows, values))
    fit = CalibrationFit(
        origin=origin,
        scale=scale,
        rms_pan_error=0.0,
        rms_tilt_error=0.0,
        points_used=len(points),
        coordinate_axes=coordinate_axes,
        projective_coefficients=coefficients,
        pan_origin=pan_origin,
        tilt_origin=tilt_origin,
    )
    residual_samples = []
    for point, pan, tilt in zip(points, pan_values, tilt_values, strict=True):
        predicted_pan, predicted_tilt, u, v = fit._predict_projective_base(point)
        residual_samples.append(
            (
                u,
                v,
                _angle_error(pan, predicted_pan),
                tilt - predicted_tilt,
            )
        )
    residual_rows = [
        [
            1.0,
            sample[0],
            sample[1],
            sample[0] ** 2,
            sample[0] * sample[1],
            sample[1] ** 2,
        ]
        for sample in residual_samples
    ]
    pan_residual_coefficients = (
        tuple(
            _least_squares(
                residual_rows, [sample[2] for sample in residual_samples]
            )
        )
        if len(residual_samples) >= 6
        else ()
    )
    tilt_residual_coefficients = (
        tuple(
            _least_squares(
                residual_rows, [sample[3] for sample in residual_samples]
            )
        )
        if len(residual_samples) >= 6
        else ()
    )
    corrected_fit = CalibrationFit(
        origin=origin,
        scale=scale,
        rms_pan_error=0.0,
        rms_tilt_error=0.0,
        points_used=len(points),
        coordinate_axes=coordinate_axes,
        projective_coefficients=coefficients,
        pan_origin=pan_origin,
        tilt_origin=tilt_origin,
        pan_residual_coefficients=pan_residual_coefficients,
        tilt_residual_coefficients=tilt_residual_coefficients,
    )
    predictions = [corrected_fit.predict(point) for point in points]
    return CalibrationFit(
        origin=origin,
        scale=scale,
        rms_pan_error=_rms(
            [_angle_error(predicted[0], pan) for predicted, pan in zip(predictions, pan_values, strict=True)]
        ),
        rms_tilt_error=_rms(
            [predicted[1] - tilt for predicted, tilt in zip(predictions, tilt_values, strict=True)]
        ),
        points_used=len(points),
        coordinate_axes=coordinate_axes,
        projective_coefficients=coefficients,
        pan_origin=pan_origin,
        tilt_origin=tilt_origin,
        pan_residual_coefficients=pan_residual_coefficients,
        tilt_residual_coefficients=tilt_residual_coefficients,
    )


def fit_inverse_xy_calibration(points: list[DrawingPoint]) -> InverseXYFit:
    taught = [point for point in points if point.calibration and point.has_pan_tilt]
    if len(taught) < 4:
        raise CalibrationError("Pan/Tilt에서 X/Y를 추정하려면 캘리브레이션 포인트가 최소 4개 필요합니다.")

    pan_values = _unwrap_angles([float(point.pan) for point in taught if point.pan is not None])
    tilt_values = [float(point.tilt) for point in taught if point.tilt is not None]
    pan_origin = sum(pan_values) / len(pan_values)
    tilt_origin = sum(tilt_values) / len(tilt_values)
    scale = max(
        max(pan_values) - min(pan_values),
        max(tilt_values) - min(tilt_values),
        1.0,
    )
    matrix = [
        (
            (pan - pan_origin) / scale,
            (tilt - tilt_origin) / scale,
            0.0,
            1.0,
        )
        for pan, tilt in zip(pan_values, tilt_values, strict=True)
    ]
    x_values = [float(point.x) for point in taught]
    y_values = [float(point.y) for point in taught]
    x_coefficients = _ridge_least_squares(matrix, x_values)
    y_coefficients = _ridge_least_squares(matrix, y_values)
    x_errors = [
        _dot4(x_coefficients, features) - target
        for features, target in zip(matrix, x_values, strict=False)
    ]
    y_errors = [
        _dot4(y_coefficients, features) - target
        for features, target in zip(matrix, y_values, strict=False)
    ]
    return InverseXYFit(
        pan_origin=pan_origin,
        tilt_origin=tilt_origin,
        scale=scale,
        x_coefficients=x_coefficients,
        y_coefficients=y_coefficients,
        rms_x_error=_rms(x_errors),
        rms_y_error=_rms(y_errors),
        points_used=len(taught),
    )


def _load_step_model(path: Path, *, fast_preview: bool = False) -> MeshModel:
    sidecar_error = ""
    sidecar = _find_sidecar_mesh(path)
    if sidecar is not None:
        try:
            model = load_model_file(sidecar)
        except (OSError, ModelLoadError, ValueError) as exc:
            sidecar_error = f"\n같은 이름의 메쉬 파일({sidecar.name})도 열지 못했습니다: {exc}"
        else:
            warnings = [
                f"STEP 대신 같은 이름의 메쉬 파일을 사용했습니다: {sidecar.name}",
                *model.warnings,
            ]
            return MeshModel(
                model.source_path,
                model.vertices,
                model.faces,
                warnings,
                model.face_colors,
            )

    cached = _load_cached_step_mesh(path)
    if cached is not None:
        return cached

    if fast_preview:
        preview = _load_step_boundary_mesh(path)
        if preview is not None:
            _save_cached_step_mesh(path, preview)
            return preview

    converted = _convert_step_with_cadquery(path) or _convert_step_with_freecad(path)
    if converted is not None:
        _save_cached_step_mesh(path, converted)
        warnings = [
            "STEP을 삼각면 메쉬로 변환해 표시합니다. 다음 로드부터는 캐시를 사용합니다.",
            *converted.warnings,
        ]
        return MeshModel(
            str(path),
            converted.vertices,
            converted.faces,
            warnings,
            converted.face_colors,
        )

    preview = _load_step_boundary_mesh(path)
    if preview is not None:
        _save_cached_step_mesh(path, preview)
        return preview

    raise ModelLoadError(
        "STP/STEP은 CAD B-Rep 형식이라 앱 내부 좌표 파싱만으로는 두 번째 사진처럼 "
        "면이 있는 3D 형상으로 표시할 수 없습니다. 같은 폴더에 같은 이름의 "
        "GLB/OBJ/STL/PLY 파일을 내보내 두거나, FreeCAD/OCCT 계열 변환기를 설치한 뒤 "
        "다시 불러와 주세요."
        f"{sidecar_error}"
    )


def _find_sidecar_mesh(path: Path) -> Path | None:
    for suffix in MESH_FILE_EXTENSIONS:
        candidate = path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    try:
        for candidate in path.parent.iterdir():
            if (
                candidate.is_file()
                and candidate.stem.casefold() == path.stem.casefold()
                and candidate.suffix.lower() in MESH_FILE_EXTENSIONS
            ):
                return candidate
    except OSError:
        return None
    return None


def _load_step_boundary_mesh(path: Path) -> MeshModel | None:
    cartesian_points: dict[int, Vector3] = {}
    vertex_points: dict[int, int] = {}
    edge_curves: dict[int, tuple[int, int]] = {}
    oriented_edges: dict[int, tuple[int, bool]] = {}
    edge_loops: dict[int, list[int]] = {}
    face_bounds: dict[int, tuple[int, bool, bool]] = {}
    advanced_face_refs: list[list[int]] = []

    try:
        _scan_step_boundary_entities(
            path,
            cartesian_points,
            vertex_points,
            edge_curves,
            oriented_edges,
            edge_loops,
            face_bounds,
            advanced_face_refs,
        )
    except OSError:
        return None

    vertices: list[Vector3] = []
    faces: list[Face] = []
    skipped_faces = 0
    for refs in advanced_face_refs:
        bounds = [face_bounds[ref] for ref in refs if ref in face_bounds]
        outer_bounds = [item for item in bounds if item[1]] or bounds[:1]
        if not outer_bounds:
            skipped_faces += 1
            continue
        added = False
        for loop_id, _is_outer, bound_sense in outer_bounds:
            polygon = _step_loop_polygon(
                loop_id,
                bound_sense,
                cartesian_points,
                vertex_points,
                edge_curves,
                oriented_edges,
                edge_loops,
            )
            if _append_step_polygon(vertices, faces, polygon):
                added = True
        if not added:
            skipped_faces += 1

    if not faces:
        return None

    vertices, faces = _dedupe_mesh(vertices, faces)
    warnings = [
        "STEP 자체 간이 B-Rep 미리보기입니다. 색상 없이 표시하며, 곡면/구멍은 단순화될 수 있습니다.",
    ]
    if skipped_faces:
        warnings.append(f"삼각화하지 못한 STEP face가 {skipped_faces:,}개 있습니다.")
    if len(faces) > 120_000:
        original_count = len(faces)
        step = math.ceil(len(faces) / 120_000)
        faces = faces[::step]
        vertices, faces = _compact_mesh(vertices, faces)
        warnings.append(
            f"표시 속도를 위해 STEP face를 {original_count:,}개에서 {len(faces):,}개로 간소화했습니다."
        )
    return MeshModel(str(path), vertices, faces, warnings)


def _scan_step_boundary_entities(
    path: Path,
    cartesian_points: dict[int, Vector3],
    vertex_points: dict[int, int],
    edge_curves: dict[int, tuple[int, int]],
    oriented_edges: dict[int, tuple[int, bool]],
    edge_loops: dict[int, list[int]],
    face_bounds: dict[int, tuple[int, bool, bool]],
    advanced_face_refs: list[list[int]],
) -> None:
    pending = ""
    with path.open("r", encoding="latin-1", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if pending:
                pending += line
            elif line.startswith("#"):
                pending = line
            else:
                continue
            if ";" not in line:
                continue
            entity = pending
            pending = ""
            parsed = _split_step_entity(entity)
            if parsed is None:
                continue
            entity_id, kind, body = parsed
            if kind == "CARTESIAN_POINT":
                point = _parse_step_coordinate_tuple(body)
                if point is not None:
                    cartesian_points[entity_id] = point
            elif kind == "VERTEX_POINT":
                refs = _step_refs(body)
                if refs:
                    vertex_points[entity_id] = refs[0]
            elif kind == "EDGE_CURVE":
                refs = _step_refs(body)
                if len(refs) >= 2:
                    edge_curves[entity_id] = (refs[0], refs[1])
            elif kind == "ORIENTED_EDGE":
                refs = _step_refs(body)
                if refs:
                    oriented_edges[entity_id] = (refs[-1], _last_step_bool(body, True))
            elif kind == "EDGE_LOOP":
                refs = _step_refs(body)
                if refs:
                    edge_loops[entity_id] = refs
            elif kind in {"FACE_OUTER_BOUND", "FACE_BOUND"}:
                refs = _step_refs(body)
                if refs:
                    face_bounds[entity_id] = (
                        refs[0],
                        kind == "FACE_OUTER_BOUND",
                        _last_step_bool(body, True),
                    )
            elif kind == "ADVANCED_FACE":
                refs = _step_refs(body)
                if refs:
                    advanced_face_refs.append(refs)


def _split_step_entity(entity: str) -> tuple[int, str, str] | None:
    head, separator, rest = entity.partition("=")
    if separator != "=" or not head.startswith("#"):
        return None
    try:
        entity_id = int(head[1:].strip())
    except ValueError:
        return None
    kind, separator, body = rest.partition("(")
    if separator != "(":
        return None
    body = body.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if body.endswith(")"):
        body = body[:-1]
    return entity_id, kind.strip().upper(), body


def _parse_step_coordinate_tuple(body: str) -> Vector3 | None:
    coordinate_text = ""
    for match in re.finditer(r"\(([^()]*)\)", body):
        coordinate_text = match.group(1)
    numbers = _parse_numbers(coordinate_text)
    if len(numbers) < 3:
        return None
    return numbers[0], numbers[1], numbers[2]


def _step_refs(body: str) -> list[int]:
    return [int(match.group(1)) for match in STEP_REF_RE.finditer(body)]


def _last_step_bool(body: str, default: bool) -> bool:
    matches = list(STEP_BOOL_RE.finditer(body))
    if not matches:
        return default
    return matches[-1].group(1).upper() == "T"


def _step_loop_polygon(
    loop_id: int,
    bound_sense: bool,
    cartesian_points: dict[int, Vector3],
    vertex_points: dict[int, int],
    edge_curves: dict[int, tuple[int, int]],
    oriented_edges: dict[int, tuple[int, bool]],
    edge_loops: dict[int, list[int]],
) -> list[Vector3]:
    polygon: list[Vector3] = []
    for oriented_id in edge_loops.get(loop_id, []):
        edge_info = oriented_edges.get(oriented_id)
        if edge_info is None:
            continue
        edge_id, orientation = edge_info
        edge = edge_curves.get(edge_id)
        if edge is None:
            continue
        start_vertex_id, end_vertex_id = edge
        if not orientation:
            start_vertex_id, end_vertex_id = end_vertex_id, start_vertex_id
        start_point = _step_vertex_position(start_vertex_id, cartesian_points, vertex_points)
        end_point = _step_vertex_position(end_vertex_id, cartesian_points, vertex_points)
        if start_point is None or end_point is None:
            continue
        if not polygon:
            polygon.append(start_point)
        elif not _same_point(polygon[-1], start_point):
            polygon.append(start_point)
        if not _same_point(polygon[-1], end_point):
            polygon.append(end_point)
    polygon = _clean_polygon(polygon)
    if not bound_sense:
        polygon.reverse()
    return polygon


def _step_vertex_position(
    vertex_id: int,
    cartesian_points: dict[int, Vector3],
    vertex_points: dict[int, int],
) -> Vector3 | None:
    point_id = vertex_points.get(vertex_id)
    if point_id is None:
        return None
    return cartesian_points.get(point_id)


def _append_step_polygon(
    vertices: list[Vector3],
    faces: list[Face],
    polygon: list[Vector3],
) -> int:
    if len(polygon) < 3:
        return 0
    offset = len(vertices)
    vertices.extend(polygon)
    added = 0
    for face in _triangulate_step_polygon(vertices, list(range(offset, offset + len(polygon)))):
        if not _triangle_is_degenerate(vertices, face):
            faces.append(face)
            added += 1
    return added


def _triangulate_step_polygon(vertices: list[Vector3], indices: list[int]) -> list[Face]:
    if len(indices) < 3:
        return []
    if len(indices) == 3:
        return [(indices[0], indices[1], indices[2])]
    points = [vertices[index] for index in indices]
    projected = _project_polygon_to_2d(points)
    area = _polygon_area_2d(projected)
    if abs(area) < 1e-12:
        return _triangulate(indices)
    work = list(range(len(indices)))
    if area < 0.0:
        work.reverse()
    triangles: list[Face] = []
    guard = 0
    while len(work) > 3 and guard < len(indices) * len(indices):
        guard += 1
        clipped = False
        for position, current in enumerate(work):
            previous = work[position - 1]
            following = work[(position + 1) % len(work)]
            if not _is_convex_ear(projected[previous], projected[current], projected[following]):
                continue
            if _ear_contains_any_point(projected, work, previous, current, following):
                continue
            triangles.append(
                (
                    indices[previous],
                    indices[current],
                    indices[following],
                )
            )
            del work[position]
            clipped = True
            break
        if not clipped:
            return _triangulate(indices)
    if len(work) == 3:
        triangles.append((indices[work[0]], indices[work[1]], indices[work[2]]))
    return triangles


def _project_polygon_to_2d(points: list[Vector3]) -> list[tuple[float, float]]:
    normal = (0.0, 0.0, 0.0)
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        normal = (
            normal[0] + (point[1] - following[1]) * (point[2] + following[2]),
            normal[1] + (point[2] - following[2]) * (point[0] + following[0]),
            normal[2] + (point[0] - following[0]) * (point[1] + following[1]),
        )
    axis = max(range(3), key=lambda item: abs(normal[item]))
    if axis == 0:
        return [(point[1], point[2]) for point in points]
    if axis == 1:
        return [(point[0], point[2]) for point in points]
    return [(point[0], point[1]) for point in points]


def _polygon_area_2d(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        total += point[0] * following[1] - following[0] * point[1]
    return total * 0.5


def _is_convex_ear(
    previous: tuple[float, float],
    current: tuple[float, float],
    following: tuple[float, float],
) -> bool:
    cross = (
        (current[0] - previous[0]) * (following[1] - current[1])
        - (current[1] - previous[1]) * (following[0] - current[0])
    )
    return cross > 1e-12


def _ear_contains_any_point(
    points: list[tuple[float, float]],
    work: list[int],
    previous: int,
    current: int,
    following: int,
) -> bool:
    a = points[previous]
    b = points[current]
    c = points[following]
    for index in work:
        if index in {previous, current, following}:
            continue
        if _point_in_triangle_2d(points[index], a, b, c):
            return True
    return False


def _point_in_triangle_2d(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    ab = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
    bc = (c[0] - b[0]) * (point[1] - b[1]) - (c[1] - b[1]) * (point[0] - b[0])
    ca = (a[0] - c[0]) * (point[1] - c[1]) - (a[1] - c[1]) * (point[0] - c[0])
    tolerance = -1e-10
    return ab >= tolerance and bc >= tolerance and ca >= tolerance


def _clean_polygon(polygon: list[Vector3]) -> list[Vector3]:
    clean: list[Vector3] = []
    for point in polygon:
        if not clean or not _same_point(clean[-1], point):
            clean.append(point)
    while len(clean) > 1 and _same_point(clean[0], clean[-1]):
        clean.pop()
    return clean


def _same_point(left: Vector3, right: Vector3) -> bool:
    return (
        abs(left[0] - right[0]) < 1e-6
        and abs(left[1] - right[1]) < 1e-6
        and abs(left[2] - right[2]) < 1e-6
    )


def _triangle_is_degenerate(vertices: list[Vector3], face: Face) -> bool:
    a, b, c = (vertices[index] for index in face)
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return cross[0] * cross[0] + cross[1] * cross[1] + cross[2] * cross[2] < 1e-12


def _compact_mesh(vertices: list[Vector3], faces: list[Face]) -> tuple[list[Vector3], list[Face]]:
    remap: dict[int, int] = {}
    compact_vertices: list[Vector3] = []
    compact_faces: list[Face] = []
    for face in faces:
        mapped: list[int] = []
        for index in face:
            compact_index = remap.get(index)
            if compact_index is None:
                compact_index = len(compact_vertices)
                remap[index] = compact_index
                compact_vertices.append(vertices[index])
            mapped.append(compact_index)
        if len(set(mapped)) == 3:
            compact_faces.append((mapped[0], mapped[1], mapped[2]))
    return compact_vertices, compact_faces


def _load_gl2(path: Path) -> MeshModel:
    prefix = path.read_bytes()[:256].lstrip()
    if prefix.startswith(b"{"):
        return _load_gltf(path)
    raise ModelLoadError(
        "이 .gl2 파일은 3D glTF가 아니라 HP-GL/2/PCL 계열 2D 플롯 데이터로 보입니다. "
        "두 번째 사진처럼 3D 면으로 보려면 CAD에서 GLB/OBJ/STL/PLY로 내보낸 파일을 "
        "불러와 주세요."
    )


def _mesh_cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "OSRND" / "PT503Tester" / "mesh_cache"


def _step_cache_path(path: Path) -> Path:
    try:
        stat = path.stat()
        identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(path)
    digest = hashlib.sha1(identity.encode("utf-8", "surrogatepass")).hexdigest()
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)[:48] or "step"
    return _mesh_cache_dir() / f"{safe_stem}-{digest}.mesh.json"


def _load_cached_step_mesh(path: Path) -> MeshModel | None:
    cache_path = _step_cache_path(path)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != CACHE_FORMAT_VERSION:
            return None
        vertices = [
            (float(item[0]), float(item[1]), float(item[2]))
            for item in data.get("vertices", [])
            if isinstance(item, list) and len(item) >= 3
        ]
        faces = [
            (int(item[0]), int(item[1]), int(item[2]))
            for item in data.get("faces", [])
            if isinstance(item, list) and len(item) >= 3
        ]
        face_colors = [
            (int(item[0]), int(item[1]), int(item[2]))
            for item in data.get("face_colors", [])
            if isinstance(item, list) and len(item) >= 3
        ]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not vertices or not faces:
        return None
    return MeshModel(
        str(path),
        vertices,
        faces,
        ["캐시된 STEP 변환 메쉬를 사용했습니다."],
        face_colors,
    )


def _save_cached_step_mesh(path: Path, model: MeshModel) -> None:
    if not model.vertices or not model.faces:
        return
    cache_path = _step_cache_path(path)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "source_path": str(path),
            "vertices": model.vertices,
            "faces": model.faces,
            "face_colors": model.face_colors,
        }
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(cache_path)
    except OSError:
        return


def _convert_step_with_cadquery(path: Path) -> MeshModel | None:
    try:
        import cadquery as cq  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "converted.stl"
            shape = cq.importers.importStep(str(path))
            try:
                cq.exporters.export(shape, str(output), tolerance=1.0, angularTolerance=0.25)
            except TypeError:
                cq.exporters.export(shape, str(output))
            if output.exists():
                return _load_stl(output)
    except Exception:
        return None
    return None


def _convert_step_with_freecad(path: Path) -> MeshModel | None:
    for command in _freecad_candidates():
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                script = Path(temp_dir) / "step_to_stl.py"
                output = Path(temp_dir) / "converted.stl"
                script.write_text(
                    """
import sys
try:
    import FreeCAD
    import Import
    import Mesh

    source_path, output_path = sys.argv[1], sys.argv[2]
    document = FreeCAD.newDocument("step_convert")
    Import.insert(source_path, document.Name)
    document.recompute()
    mesh = Mesh.Mesh()
    for item in document.Objects:
        shape = getattr(item, "Shape", None)
        if shape is None or shape.isNull():
            continue
        mesh.addMesh(Mesh.Mesh(shape.tessellate(1.0)))
    if mesh.CountFacets == 0:
        raise RuntimeError("no facets created")
    mesh.write(output_path)
except Exception as exc:
    sys.stderr.write(str(exc))
    sys.exit(1)
""".strip(),
                    encoding="utf-8",
                )
                subprocess.run(
                    [str(command), str(script), str(path), str(output)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if output.exists():
                    return _load_stl(output)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def _freecad_candidates() -> list[Path]:
    candidates: list[Path] = []
    for command in ("FreeCADCmd", "FreeCADCmd.exe"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(env_name)
        if not root:
            continue
        try:
            candidates.extend(Path(root).glob("FreeCAD*/bin/FreeCADCmd.exe"))
            candidates.extend(Path(root).glob("FreeCAD*/FreeCADCmd.exe"))
        except OSError:
            continue
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen and candidate.exists():
            seen.add(key)
            unique.append(candidate)
    return unique


def _load_step_points(path: Path) -> MeshModel:
    text = _read_text(path)
    vertices: list[Vector3] = []
    for match in STEP_POINT_RE.finditer(text):
        numbers = _parse_numbers(match.group(1))
        if len(numbers) >= 3:
            vertices.append((numbers[0], numbers[1], numbers[2]))
    vertices = _unique_vertices(vertices)
    return MeshModel(
        str(path),
        vertices,
        [],
        ["STEP/STP는 표면 삼각분할 정보가 없어 좌표 포인트 클라우드로 표시합니다."],
    )


def _load_obj(path: Path) -> MeshModel:
    vertices: list[Vector3] = []
    faces: list[Face] = []
    warnings: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("v "):
                numbers = _parse_numbers(stripped[1:])
                if len(numbers) >= 3:
                    vertices.append((numbers[0], numbers[1], numbers[2]))
            elif stripped.startswith("f "):
                indices: list[int] = []
                for token in stripped[1:].split():
                    raw = token.split("/", 1)[0]
                    if not raw:
                        continue
                    index = int(raw)
                    if index < 0:
                        index = len(vertices) + index + 1
                    indices.append(index - 1)
                faces.extend(_triangulate(indices))
    if not faces:
        warnings.append("OBJ 면 정보가 없어 점/와이어 중심으로 표시합니다.")
    return MeshModel(str(path), vertices, faces, warnings)


def _load_stl(path: Path) -> MeshModel:
    data = path.read_bytes()
    if len(data) >= 84:
        triangle_count = struct.unpack_from("<I", data, 80)[0]
        expected_size = 84 + triangle_count * 50
        if triangle_count > 0 and expected_size <= len(data):
            return _load_binary_stl(path, data, triangle_count)
    return _load_ascii_stl(path)


def _load_binary_stl(path: Path, data: bytes, triangle_count: int) -> MeshModel:
    vertices: list[Vector3] = []
    faces: list[Face] = []
    offset = 84
    for _ in range(triangle_count):
        face_indices: list[int] = []
        offset += 12
        for _vertex in range(3):
            vertex = struct.unpack_from("<fff", data, offset)
            offset += 12
            vertices.append((float(vertex[0]), float(vertex[1]), float(vertex[2])))
            face_indices.append(len(vertices) - 1)
        faces.append((face_indices[0], face_indices[1], face_indices[2]))
        offset += 2
    vertices, faces = _dedupe_mesh(vertices, faces)
    return MeshModel(str(path), vertices, faces, [])


def _load_ascii_stl(path: Path) -> MeshModel:
    vertices: list[Vector3] = []
    faces: list[Face] = []
    current: list[int] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip().lower()
            if stripped.startswith("vertex"):
                numbers = _parse_numbers(line)
                if len(numbers) >= 3:
                    vertices.append((numbers[0], numbers[1], numbers[2]))
                    current.append(len(vertices) - 1)
                    if len(current) == 3:
                        faces.append((current[0], current[1], current[2]))
                        current = []
    vertices, faces = _dedupe_mesh(vertices, faces)
    return MeshModel(str(path), vertices, faces, [])


def _load_ply(path: Path) -> MeshModel:
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        first_line = handle.readline().strip()
        if first_line != "ply":
            raise ModelLoadError("PLY 헤더를 찾지 못했습니다.")
        vertex_count = 0
        face_count = 0
        ascii_format = False
        for line in handle:
            stripped = line.strip()
            if stripped == "format ascii 1.0":
                ascii_format = True
            elif stripped.startswith("element vertex"):
                vertex_count = int(stripped.split()[-1])
            elif stripped.startswith("element face"):
                face_count = int(stripped.split()[-1])
            elif stripped == "end_header":
                break
        if not ascii_format:
            raise ModelLoadError("현재 PLY는 ASCII 형식만 바로 표시할 수 있습니다.")
        vertices: list[Vector3] = []
        faces: list[Face] = []
        for _ in range(vertex_count):
            numbers = _parse_numbers(handle.readline())
            if len(numbers) >= 3:
                vertices.append((numbers[0], numbers[1], numbers[2]))
        for _ in range(face_count):
            parts = handle.readline().split()
            if not parts:
                continue
            count = int(parts[0])
            indices = [int(value) for value in parts[1 : 1 + count]]
            faces.extend(_triangulate(indices))
    return MeshModel(str(path), vertices, faces, [])


def _load_glb(path: Path) -> MeshModel:
    data = path.read_bytes()
    if len(data) < 20:
        raise ModelLoadError("GLB 파일이 너무 짧습니다.")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67 or version != 2 or length != len(data):
        raise ModelLoadError("GLB 2.0 형식이 아닙니다.")
    offset = 12
    json_chunk: bytes | None = None
    bin_chunk = b""
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_length]
        offset += chunk_length
        if chunk_type == 0x4E4F534A:
            json_chunk = chunk
        elif chunk_type == 0x004E4942:
            bin_chunk = chunk
    if json_chunk is None:
        raise ModelLoadError("GLB JSON 청크를 찾지 못했습니다.")
    document = json.loads(json_chunk.decode("utf-8"))
    return _geometry_from_gltf_document(path, document, bin_chunk)


def _load_gltf(path: Path) -> MeshModel:
    document = json.loads(_read_text(path))
    return _geometry_from_gltf_document(path, document, None)


def _geometry_from_gltf_document(
    path: Path,
    document: dict[str, Any],
    embedded_bin: bytes | None,
) -> MeshModel:
    buffers = _load_gltf_buffers(path, document, embedded_bin)
    vertices: list[Vector3] = []
    faces: list[Face] = []
    face_colors: list[FaceColor] = []
    warnings: list[str] = []
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            attributes = primitive.get("attributes", {})
            position_accessor = attributes.get("POSITION")
            if position_accessor is None:
                continue
            material_color = _gltf_material_color(document, primitive)
            positions = _read_gltf_accessor(document, buffers, int(position_accessor))
            local_vertices = [
                (float(item[0]), float(item[1]), float(item[2]))
                for item in positions
                if len(item) >= 3
            ]
            vertex_offset = len(vertices)
            vertices.extend(local_vertices)
            mode = int(primitive.get("mode", 4))
            indices_accessor = primitive.get("indices")
            indices: list[int]
            if indices_accessor is not None:
                indices = [
                    int(item[0])
                    for item in _read_gltf_accessor(document, buffers, int(indices_accessor))
                    if item
                ]
            else:
                indices = list(range(len(local_vertices)))
            if mode == 4:
                for start in range(0, len(indices) - 2, 3):
                    faces.append(
                        (
                            vertex_offset + indices[start],
                            vertex_offset + indices[start + 1],
                            vertex_offset + indices[start + 2],
                        )
                    )
                    face_colors.append(material_color)
            else:
                warnings.append(f"GLTF primitive mode {mode}는 점 표시로 처리했습니다.")
    if not faces:
        warnings.append("GLTF/GLB 면 정보가 없어 점 중심으로 표시합니다.")
    return MeshModel(str(path), vertices, faces, warnings, face_colors)


def _gltf_material_color(document: dict[str, Any], primitive: dict[str, Any]) -> FaceColor:
    material_index = primitive.get("material")
    if material_index is None:
        return 142, 154, 177
    try:
        material = document.get("materials", [])[int(material_index)]
        pbr = material.get("pbrMetallicRoughness", {})
        factor = pbr.get("baseColorFactor", [0.56, 0.61, 0.70, 1.0])
        return (
            _color_factor_to_byte(factor[0]),
            _color_factor_to_byte(factor[1]),
            _color_factor_to_byte(factor[2]),
        )
    except (IndexError, TypeError, ValueError):
        return 142, 154, 177


def _color_factor_to_byte(value: Any) -> int:
    number = float(value)
    if 0.0 <= number <= 1.0:
        number *= 255.0
    return int(max(0.0, min(255.0, round(number))))


def _load_gltf_buffers(
    path: Path,
    document: dict[str, Any],
    embedded_bin: bytes | None,
) -> list[bytes]:
    buffers: list[bytes] = []
    for index, buffer_info in enumerate(document.get("buffers", [])):
        uri = buffer_info.get("uri")
        if uri is None:
            if embedded_bin is None:
                raise ModelLoadError("외부 buffer URI가 없는 GLTF buffer를 읽을 수 없습니다.")
            buffers.append(embedded_bin)
            continue
        if uri.startswith("data:"):
            _, _, payload = uri.partition(",")
            buffers.append(base64.b64decode(payload))
        else:
            buffers.append((path.parent / uri).read_bytes())
        expected = buffer_info.get("byteLength")
        if expected is not None and len(buffers[-1]) < int(expected):
            raise ModelLoadError(f"GLTF buffer {index} 길이가 부족합니다.")
    return buffers


def _read_gltf_accessor(
    document: dict[str, Any],
    buffers: list[bytes],
    accessor_index: int,
) -> list[tuple[float, ...]]:
    accessor = document["accessors"][accessor_index]
    if "sparse" in accessor:
        raise ModelLoadError("Sparse GLTF accessor는 아직 지원하지 않습니다.")
    buffer_view = document["bufferViews"][int(accessor["bufferView"])]
    buffer_data = buffers[int(buffer_view.get("buffer", 0))]
    component_type = int(accessor["componentType"])
    accessor_type = str(accessor["type"])
    if component_type not in COMPONENT_FORMATS or accessor_type not in ACCESSOR_COMPONENTS:
        raise ModelLoadError("지원하지 않는 GLTF accessor 형식입니다.")
    fmt, component_size = COMPONENT_FORMATS[component_type]
    component_count = ACCESSOR_COMPONENTS[accessor_type]
    item_size = component_size * component_count
    stride = int(buffer_view.get("byteStride", item_size))
    offset = int(buffer_view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    count = int(accessor["count"])
    values: list[tuple[float, ...]] = []
    unpack_format = "<" + fmt * component_count
    for item_index in range(count):
        item_offset = offset + item_index * stride
        item = struct.unpack_from(unpack_format, buffer_data, item_offset)
        values.append(tuple(float(component) for component in item))
    return values


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "cp949", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _parse_numbers(text: str) -> list[float]:
    return [
        float(value.replace("D", "E").replace("d", "e"))
        for value in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][-+]?\d+)?", text)
    ]


def _triangulate(indices: list[int]) -> list[Face]:
    if len(indices) < 3:
        return []
    return [(indices[0], indices[i], indices[i + 1]) for i in range(1, len(indices) - 1)]


def _unique_vertices(vertices: list[Vector3]) -> list[Vector3]:
    unique: list[Vector3] = []
    seen: set[tuple[int, int, int]] = set()
    for vertex in vertices:
        key = tuple(round(component * 1_000_000) for component in vertex)
        if key not in seen:
            seen.add(key)
            unique.append(vertex)
    return unique


def _dedupe_mesh(vertices: list[Vector3], faces: list[Face]) -> tuple[list[Vector3], list[Face]]:
    unique: list[Vector3] = []
    remap: dict[int, int] = {}
    seen: dict[tuple[int, int, int], int] = {}
    for index, vertex in enumerate(vertices):
        key = tuple(round(component * 1_000_000) for component in vertex)
        mapped = seen.get(key)
        if mapped is None:
            mapped = len(unique)
            seen[key] = mapped
            unique.append(vertex)
        remap[index] = mapped
    return unique, [(remap[a], remap[b], remap[c]) for a, b, c in faces]


def _calibration_origin_scale(points: list[DrawingPoint]) -> tuple[Vector3, float]:
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    zs = [point.z for point in points]
    origin = (
        sum(xs) / len(xs),
        sum(ys) / len(ys),
        sum(zs) / len(zs),
    )
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1.0)
    return origin, float(span)


def _planar_coordinate_axes(points: list[DrawingPoint]) -> tuple[int, int] | None:
    coordinates = list(zip(*(point.position for point in points), strict=True))
    spans = [max(axis) - min(axis) for axis in coordinates]
    ordered = sorted(range(3), key=lambda index: spans[index], reverse=True)
    if spans[ordered[1]] <= max(spans[ordered[0]], 1.0) * 1e-6:
        raise CalibrationError("캘리브레이션 기준점을 서로 멀리 떨어진 위치로 선택하세요.")
    if spans[ordered[2]] > max(spans[ordered[0]], 1.0) * 1e-6:
        return None
    return ordered[0], ordered[1]


def _motor_basis(pan_origin: float, tilt_origin: float) -> tuple[Vector3, Vector3, Vector3]:
    pan = math.radians(pan_origin)
    tilt = math.radians(tilt_origin)
    forward = (
        math.cos(tilt) * math.sin(pan),
        math.sin(tilt),
        math.cos(tilt) * math.cos(pan),
    )
    right = (math.cos(pan), 0.0, -math.sin(pan))
    up = (
        -math.sin(tilt) * math.sin(pan),
        math.cos(tilt),
        -math.sin(tilt) * math.cos(pan),
    )
    return right, up, forward


def _motor_image_coordinates(
    pan: float,
    tilt: float,
    pan_origin: float,
    tilt_origin: float,
) -> tuple[float, float]:
    pan_radians = math.radians(pan)
    tilt_radians = math.radians(tilt)
    direction = (
        math.cos(tilt_radians) * math.sin(pan_radians),
        math.sin(tilt_radians),
        math.cos(tilt_radians) * math.cos(pan_radians),
    )
    right, up, forward = _motor_basis(pan_origin, tilt_origin)
    local_x = sum(a * b for a, b in zip(direction, right, strict=True))
    local_y = sum(a * b for a, b in zip(direction, up, strict=True))
    local_z = sum(a * b for a, b in zip(direction, forward, strict=True))
    if local_z <= 1e-6:
        raise CalibrationError("캘리브레이션 각도 범위가 너무 넓습니다.")
    return local_x / local_z, local_y / local_z


def _motor_direction_from_image(
    image_x: float,
    image_y: float,
    pan_origin: float,
    tilt_origin: float,
) -> Vector3:
    right, up, forward = _motor_basis(pan_origin, tilt_origin)
    return tuple(
        image_x * right[index] + image_y * up[index] + forward[index]
        for index in range(3)
    )  # type: ignore[return-value]


def _normalized_features(position: Vector3, origin: Vector3, scale: float) -> tuple[float, float, float, float]:
    return (
        (position[0] - origin[0]) / scale,
        (position[1] - origin[1]) / scale,
        (position[2] - origin[2]) / scale,
        1.0,
    )


def _ridge_least_squares(
    matrix: list[tuple[float, float, float, float]],
    values: list[float],
) -> tuple[float, float, float, float]:
    normal = [[0.0 for _ in range(4)] for _ in range(4)]
    vector = [0.0 for _ in range(4)]
    for row, value in zip(matrix, values, strict=False):
        for i in range(4):
            vector[i] += row[i] * value
            for j in range(4):
                normal[i][j] += row[i] * row[j]
    diagonal_scale = max(max(abs(normal[i][i]) for i in range(4)), 1.0)
    for i in range(4):
        normal[i][i] += diagonal_scale * 1e-9
    return tuple(_solve_linear(normal, vector))  # type: ignore[return-value]


def _least_squares(matrix: list[list[float]], values: list[float]) -> list[float]:
    if not matrix or len(matrix) != len(values):
        raise CalibrationError("캘리브레이션 데이터가 비어 있습니다.")
    width = len(matrix[0])
    if any(len(row) != width for row in matrix):
        raise CalibrationError("캘리브레이션 데이터 크기가 일치하지 않습니다.")
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    vector = [0.0 for _ in range(width)]
    for row, value in zip(matrix, values, strict=True):
        for i in range(width):
            vector[i] += row[i] * value
            for j in range(width):
                normal[i][j] += row[i] * row[j]
    diagonal_scale = max(max(abs(normal[i][i]) for i in range(width)), 1.0)
    for i in range(width):
        normal[i][i] += diagonal_scale * 1e-12
    try:
        return _solve_linear(normal, vector)
    except CalibrationError as exc:
        raise CalibrationError(
            "캘리브레이션 기준점을 도면 영역의 모서리 방향으로 넓게 배치하세요."
        ) from exc


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    rows = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise CalibrationError("캘리브레이션 포인트 배치가 너무 한쪽으로 몰려 계산이 불안정합니다.")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        pivot_value = rows[column][column]
        rows[column] = [value / pivot_value for value in rows[column]]
        for row_index in range(size):
            if row_index == column:
                continue
            factor = rows[row_index][column]
            rows[row_index] = [
                value - factor * rows[column][item]
                for item, value in enumerate(rows[row_index])
            ]
    return [rows[row][size] for row in range(size)]


def _unwrap_angles(values: list[float]) -> list[float]:
    if not values:
        return []
    base = values[0]
    return [base + ((value - base + 180.0) % 360.0) - 180.0 for value in values]


def _angle_error(value: float, target: float) -> float:
    return ((value - target + 180.0) % 360.0) - 180.0


def _dot4(coefficients: tuple[float, float, float, float], features: tuple[float, float, float, float]) -> float:
    return sum(a * b for a, b in zip(coefficients, features, strict=True))


def _rms(values: list[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


class DrawingViewer(QWidget):
    """Lightweight Qt-painted 3D viewer with mouse picking."""

    point_picked = Signal(object)

    MAX_DRAW_FACES = 100_000
    MAX_DRAW_WIREFRAME_FACES = 12_000
    MAX_PICK_FACES = 35_000
    MAX_DRAW_POINTS = 15000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(560, 420)
        self.setMouseTracking(True)
        self.model: MeshModel | None = None
        self.points: list[DrawingPoint] = []
        self.selected_point_id = ""
        self.render_mode = "shaded"
        self.yaw = -35.0
        self.pitch = 22.0
        self.zoom = 1.0
        self.pan = QPointF(0.0, 0.0)
        self._press_pos = QPoint()
        self._last_pos = QPoint()
        self._dragged = False
        self._bounds_center: Vector3 = (0.0, 0.0, 0.0)
        self._bounds_scale = 1.0

    def set_model(self, model: MeshModel | None) -> None:
        self.model = model
        self._update_bounds()
        self.reset_view()

    def set_points(self, points: list[DrawingPoint]) -> None:
        self.points = points
        self.update()

    def set_selected_point(self, point_id: str) -> None:
        self.selected_point_id = point_id
        self.update()

    def set_render_mode(self, mode: str) -> None:
        self.render_mode = mode
        self.update()

    def reset_view(self) -> None:
        self.yaw = -35.0
        self.pitch = 22.0
        self.zoom = 1.0
        self.pan = QPointF(0.0, 0.0)
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#10151d"))
        self._draw_background(painter)
        if self.model is None:
            painter.setPen(QColor("#8fa3ba"))
            painter.setFont(QFont("Segoe UI", 12))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "도면 파일을 불러오세요")
            return
        projected = self._project_vertices(self.model.vertices)
        self._draw_model(painter, projected)
        self._draw_axes(painter)
        self._draw_points(painter)
        self._draw_status(painter)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press_pos = event.position().toPoint()
        self._last_pos = self._press_pos
        self._dragged = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        current = event.position().toPoint()
        delta = current - self._last_pos
        if (current - self._press_pos).manhattanLength() > 4:
            self._dragged = True
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.yaw += delta.x() * 0.45
            self.pitch = max(-89.0, min(89.0, self.pitch + delta.y() * 0.45))
            self.update()
        elif event.buttons() & Qt.MouseButton.RightButton:
            self.pan += QPointF(float(delta.x()), float(delta.y()))
            self.update()
        self._last_pos = current

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._dragged
            and self.model is not None
        ):
            picked = self._pick(event.position())
            if picked is not None:
                self.point_picked.emit(picked)

    def mouseDoubleClickEvent(self, _event: QMouseEvent) -> None:
        self.reset_view()

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120.0
        self.zoom = max(0.08, min(50.0, self.zoom * (1.12**steps)))
        self.update()

    def _update_bounds(self) -> None:
        if self.model is None or not self.model.vertices:
            self._bounds_center = (0.0, 0.0, 0.0)
            self._bounds_scale = 1.0
            return
        minimum, maximum = self.model.bounds
        self._bounds_center = (
            (minimum[0] + maximum[0]) * 0.5,
            (minimum[1] + maximum[1]) * 0.5,
            (minimum[2] + maximum[2]) * 0.5,
        )
        self._bounds_scale = max(
            maximum[0] - minimum[0],
            maximum[1] - minimum[1],
            maximum[2] - minimum[2],
            1.0,
        )

    def _draw_background(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor("#1b2430"), 1))
        spacing = 40
        for x in range(0, self.width(), spacing):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), spacing):
            painter.drawLine(0, y, self.width(), y)

    def _draw_model(
        self,
        painter: QPainter,
        projected: list[tuple[QPointF, float, Vector3]],
    ) -> None:
        if self.model is None:
            return
        mode = self.render_mode
        draw_faces = bool(self.model.faces) and mode not in {"wireframe", "points"}
        draw_edges = mode in {"wireframe", "xray"}
        if draw_faces:
            self._draw_faces(painter, projected, mode)
        if draw_edges and self.model.faces:
            self._draw_wireframe(painter, projected)
        if mode == "points" or not self.model.faces:
            self._draw_vertex_cloud(painter, projected)

    def _draw_faces(
        self,
        painter: QPainter,
        projected: list[tuple[QPointF, float, Vector3]],
        mode: str,
    ) -> None:
        assert self.model is not None
        step = max(1, len(self.model.faces) // self.MAX_DRAW_FACES)
        items: list[tuple[float, QPolygonF, QColor]] = []
        minimum, maximum = self.model.bounds
        z_span = max(maximum[2] - minimum[2], 1.0)
        for face_index in range(0, len(self.model.faces), step):
            face = self.model.faces[face_index]
            a, b, c = face
            pa, za, ta = projected[a]
            pb, zb, tb = projected[b]
            pc, zc, tc = projected[c]
            depth = (za + zb + zc) / 3.0
            polygon = QPolygonF([pa, pb, pc])
            material = (
                self.model.face_colors[face_index]
                if face_index < len(self.model.face_colors)
                else None
            )
            if mode == "height":
                z_level = (((ta[2] + tb[2] + tc[2]) / 3.0) - minimum[2]) / z_span
                color = QColor.fromHsvF(0.58 - 0.52 * z_level, 0.62, 0.88, 0.86)
            elif mode == "xray":
                if material is None:
                    color = QColor(103, 183, 255, 54)
                else:
                    color = QColor(material[0], material[1], material[2], 72)
            else:
                if material is None:
                    shade = int(max(80, min(210, 128 + depth * 45.0)))
                    color = QColor(shade, min(235, shade + 24), min(255, shade + 42), 245)
                else:
                    factor = max(0.62, min(1.22, 0.88 + depth * 0.30))
                    color = QColor(
                        min(255, int(material[0] * factor)),
                        min(255, int(material[1] * factor)),
                        min(255, int(material[2] * factor)),
                        245,
                    )
            items.append((depth, polygon, color))
        painter.setPen(Qt.PenStyle.NoPen)
        for _depth, polygon, color in sorted(items, key=lambda item: item[0]):
            painter.setBrush(color)
            painter.drawPolygon(polygon)

    def _draw_wireframe(
        self,
        painter: QPainter,
        projected: list[tuple[QPointF, float, Vector3]],
    ) -> None:
        assert self.model is not None
        step = max(1, len(self.model.faces) // self.MAX_DRAW_WIREFRAME_FACES)
        painter.setPen(QPen(QColor("#9dc8ff"), 1))
        for face in self.model.faces[::step]:
            points = [projected[index][0] for index in face]
            painter.drawLine(points[0], points[1])
            painter.drawLine(points[1], points[2])
            painter.drawLine(points[2], points[0])

    def _draw_vertex_cloud(
        self,
        painter: QPainter,
        projected: list[tuple[QPointF, float, Vector3]],
    ) -> None:
        step = max(1, len(projected) // self.MAX_DRAW_POINTS)
        painter.setPen(QPen(QColor("#95b6d6"), 2))
        for screen, _depth, _original in projected[::step]:
            painter.drawPoint(screen)

    def _draw_axes(self, painter: QPainter) -> None:
        origin = self._project(self._bounds_center)[0]
        axis_length = self._bounds_scale * 0.18
        axes = (
            ((self._bounds_center[0] + axis_length, self._bounds_center[1], self._bounds_center[2]), QColor("#ff6961"), "X"),
            ((self._bounds_center[0], self._bounds_center[1] + axis_length, self._bounds_center[2]), QColor("#7bd88f"), "Y"),
            ((self._bounds_center[0], self._bounds_center[1], self._bounds_center[2] + axis_length), QColor("#74b9ff"), "Z"),
        )
        font = QFont("Segoe UI", 9)
        font.setBold(True)
        painter.setFont(font)
        for endpoint, color, label in axes:
            screen = self._project(endpoint)[0]
            painter.setPen(QPen(color, 2))
            painter.drawLine(origin, screen)
            painter.drawText(screen + QPointF(5.0, -5.0), label)

    def _draw_points(self, painter: QPainter) -> None:
        font = QFont("Segoe UI", 9)
        font.setBold(True)
        painter.setFont(font)
        for index, point in enumerate(self.points, start=1):
            screen, _depth, _original = self._project(point.position)
            selected = point.id == self.selected_point_id
            color = QColor("#ffd166") if point.calibration else QColor("#4dd4ac")
            if not point.has_pan_tilt:
                color = QColor("#ff7f7f") if point.calibration else QColor("#f2f2f2")
            radius = 7 if selected else 5
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#ffffff") if selected else QColor("#151b22"), 2))
            painter.drawEllipse(screen, radius, radius)
            painter.setPen(QColor("#eaf4ff"))
            painter.drawText(screen + QPointF(8.0, -8.0), str(index))

    def _draw_status(self, painter: QPainter) -> None:
        if self.model is None:
            return
        painter.setPen(QColor("#b9c7d8"))
        painter.setFont(QFont("Segoe UI", 9))
        face_count = len(self.model.faces)
        text = f"{self.model.name}  |  vertices {len(self.model.vertices):,}  |  faces {face_count:,}"
        painter.drawText(12, self.height() - 14, text)

    def _project(self, vertex: Vector3) -> tuple[QPointF, float, Vector3]:
        rotated = self._rotate(vertex)
        distance = 4.0
        factor = distance / max(0.25, distance - rotated[2])
        base = min(self.width(), self.height()) * 0.62 * self.zoom
        screen = QPointF(
            self.width() * 0.5 + self.pan.x() + rotated[0] * base * factor,
            self.height() * 0.5 + self.pan.y() - rotated[1] * base * factor,
        )
        return screen, rotated[2], vertex

    def _rotate(self, vertex: Vector3) -> Vector3:
        x = (vertex[0] - self._bounds_center[0]) / self._bounds_scale
        y = (vertex[1] - self._bounds_center[1]) / self._bounds_scale
        z = (vertex[2] - self._bounds_center[2]) / self._bounds_scale
        yaw = math.radians(self.yaw)
        pitch = math.radians(self.pitch)
        x1 = x * math.cos(yaw) - y * math.sin(yaw)
        y1 = x * math.sin(yaw) + y * math.cos(yaw)
        z1 = z
        y2 = y1 * math.cos(pitch) - z1 * math.sin(pitch)
        z2 = y1 * math.sin(pitch) + z1 * math.cos(pitch)
        return x1, y2, z2

    def _pick(self, position: QPointF) -> Vector3 | None:
        if self.model is None or not self.model.vertices:
            return None
        projected = self._project_vertices(self.model.vertices)
        if self.model.faces:
            picked = self._pick_face(position, projected)
            if picked is not None:
                return picked
        return self._pick_vertex(position, projected)

    def _project_vertices(
        self,
        vertices: list[Vector3],
    ) -> list[tuple[QPointF, float, Vector3]]:
        yaw = math.radians(self.yaw)
        pitch = math.radians(self.pitch)
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)
        cos_pitch = math.cos(pitch)
        sin_pitch = math.sin(pitch)
        scale = self._bounds_scale
        center = self._bounds_center
        base = min(self.width(), self.height()) * 0.62 * self.zoom
        origin_x = self.width() * 0.5 + self.pan.x()
        origin_y = self.height() * 0.5 + self.pan.y()
        distance = 4.0
        projected: list[tuple[QPointF, float, Vector3]] = []
        for vertex in vertices:
            x = (vertex[0] - center[0]) / scale
            y = (vertex[1] - center[1]) / scale
            z = (vertex[2] - center[2]) / scale
            x1 = x * cos_yaw - y * sin_yaw
            y1 = x * sin_yaw + y * cos_yaw
            y2 = y1 * cos_pitch - z * sin_pitch
            z2 = y1 * sin_pitch + z * cos_pitch
            factor = distance / max(0.25, distance - z2)
            screen = QPointF(origin_x + x1 * base * factor, origin_y - y2 * base * factor)
            projected.append((screen, z2, vertex))
        return projected

    def _pick_face(
        self,
        position: QPointF,
        projected: list[tuple[QPointF, float, Vector3]],
    ) -> Vector3 | None:
        assert self.model is not None
        step = max(1, len(self.model.faces) // self.MAX_PICK_FACES)
        best_depth = -math.inf
        best_point: Vector3 | None = None
        for face in self.model.faces[::step]:
            items = [projected[index] for index in face]
            weights = _barycentric(position, items[0][0], items[1][0], items[2][0])
            if weights is None:
                continue
            depth = sum(weights[index] * items[index][1] for index in range(3))
            if depth > best_depth:
                best_depth = depth
                best_point = (
                    sum(weights[index] * items[index][2][0] for index in range(3)),
                    sum(weights[index] * items[index][2][1] for index in range(3)),
                    sum(weights[index] * items[index][2][2] for index in range(3)),
                )
        return best_point

    @staticmethod
    def _pick_vertex(
        position: QPointF,
        projected: list[tuple[QPointF, float, Vector3]],
    ) -> Vector3 | None:
        best_distance = 28.0
        best_depth = -math.inf
        best_vertex: Vector3 | None = None
        for screen, depth, vertex in projected:
            distance = math.hypot(screen.x() - position.x(), screen.y() - position.y())
            if distance <= best_distance and depth >= best_depth:
                best_distance = distance
                best_depth = depth
                best_vertex = vertex
        return best_vertex


def _barycentric(point: QPointF, a: QPointF, b: QPointF, c: QPointF) -> tuple[float, float, float] | None:
    denominator = (b.y() - c.y()) * (a.x() - c.x()) + (c.x() - b.x()) * (a.y() - c.y())
    if abs(denominator) < 1e-9:
        return None
    w1 = ((b.y() - c.y()) * (point.x() - c.x()) + (c.x() - b.x()) * (point.y() - c.y())) / denominator
    w2 = ((c.y() - a.y()) * (point.x() - c.x()) + (a.x() - c.x()) * (point.y() - c.y())) / denominator
    w3 = 1.0 - w1 - w2
    tolerance = -0.015
    if w1 >= tolerance and w2 >= tolerance and w3 >= tolerance:
        return w1, w2, w3
    return None

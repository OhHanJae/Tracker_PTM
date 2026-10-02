"""Server-side drawing upload, mesh cache, and calibration workflow storage."""

from __future__ import annotations

import json
import math
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .drawing_calibration import (
    CalibrationError,
    DrawingPoint,
    MeshModel,
    fit_affine_calibration,
    fit_inverse_xy_calibration,
    load_model_file,
)
from .recipe_store import RecipeStore, app_state_dir, now_iso

SUPPORTED_DRAWING_EXTENSIONS = {
    ".stl",
    ".stp",
    ".step",
    ".cad",
    ".obj",
    ".ply",
    ".glb",
    ".gltf",
}
STEP_SOURCE_EXTENSIONS = {".stp", ".step", ".cad"}
SIDE_CAR_MESH_EXTENSIONS = {".glb", ".gltf", ".obj", ".stl", ".ply"}
CAD_PACKAGE_KINDS = {"cad_package", "uploaded_cad_package"}
MAX_WEB_FACES = 80_000
MAX_WEB_VERTICES = 160_000
CAD_POINT_METADATA = (
    "target_id",
    "source_point_id",
    "group_id",
    "group_name",
    "status",
    "contact_z_certified",
    "possible_non_fastening_features",
    "equipment_region",
    "equipment_sequence",
)


class DrawingStore:
    """JSON-backed drawing workflow store.

    The original upload and the generated lightweight mesh are kept on disk.
    Points stay separate from motion recipes until an explicit import/export.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        package_root: str | Path | None = None,
    ) -> None:
        explicit_root = root is not None
        self.root = Path(root) if explicit_root else app_state_dir() / "drawings"
        self.package_root = (
            Path(package_root)
            if package_root is not None
            else (
                None
                if explicit_root
                else Path(__file__).resolve().parents[1] / "data" / "drawning"
            )
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "drawings.json"
        self._drawings: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        if not self.manifest_path.exists():
            self._drawings = []
        else:
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._drawings = []
            else:
                drawings = data.get("drawings", []) if isinstance(data, dict) else []
                self._drawings = [item for item in drawings if isinstance(item, dict)]
        self._sync_cad_packages()

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated": now_iso(),
            "drawings": self._drawings,
        }
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.manifest_path)

    def list_drawings(self) -> list[dict[str, Any]]:
        return [
            self._public_record(record, include_points=False)
            for record in sorted(
                self._drawings,
                key=lambda item: str(item.get("updated", "")),
                reverse=True,
            )
        ]

    def get_drawing(self, drawing_id: str) -> dict[str, Any]:
        return self._public_record(self._record(drawing_id), include_points=True)

    def create_from_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        safe_name = _safe_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_DRAWING_EXTENSIONS:
            raise ValueError(
                "지원 도면 형식은 STL, STP, STEP, CAD, OBJ, PLY, GLB, GLTF입니다."
            )

        drawing_id = str(uuid.uuid4())
        drawing_dir = self.root / drawing_id
        drawing_dir.mkdir(parents=True, exist_ok=False)
        source_path = drawing_dir / safe_name
        mesh_path = drawing_dir / "mesh.json"

        try:
            source_path.write_bytes(data)
            model = load_model_file(source_path, fast_step=True)
            mesh_payload = _mesh_payload(model)
            mesh_path.write_text(
                json.dumps(mesh_payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(drawing_dir, ignore_errors=True)
            raise

        now = now_iso()
        record = {
            "id": drawing_id,
            "name": Path(safe_name).stem or safe_name,
            "source_filename": safe_name,
            "source_path": str(source_path),
            "mesh_path": str(mesh_path),
            "created": now,
            "updated": now,
            "warnings": list(model.warnings),
            "bounds": _bounds_dict(model.bounds),
            "vertex_count": len(model.vertices),
            "face_count": len(model.faces),
            "web_vertex_count": len(mesh_payload["vertices"]),
            "web_face_count": len(mesh_payload["faces"]),
            "points": [],
            "calibration": None,
        }
        self._drawings.append(record)
        self.save()
        return self._public_record(record, include_points=True)

    def create_from_uploads(self, files: list[tuple[str, bytes]]) -> dict[str, Any]:
        if any(_safe_filename(filename) == "coordinates_manifest.json" for filename, _ in files):
            return self._create_cad_package_from_uploads(files)

        uploads: list[dict[str, Any]] = []
        used_names: set[str] = set()

        for filename, data in files:
            safe_name = _unique_filename(_safe_filename(filename), used_names)
            suffix = Path(safe_name).suffix.lower()
            if suffix not in SUPPORTED_DRAWING_EXTENSIONS:
                raise ValueError(
                    "Supported drawing files are STL, STP, STEP, CAD, OBJ, PLY, GLB, and GLTF."
                )
            if not data:
                raise ValueError("Uploaded drawing file is empty.")
            uploads.append(
                {
                    "name": safe_name,
                    "suffix": suffix,
                    "data": data,
                }
            )

        if not uploads:
            raise ValueError("Select at least one drawing file.")

        primary_upload = next(
            (
                upload
                for upload in uploads
                if upload["suffix"] in STEP_SOURCE_EXTENSIONS
            ),
            uploads[0],
        )

        drawing_id = str(uuid.uuid4())
        drawing_dir = self.root / drawing_id
        drawing_dir.mkdir(parents=True, exist_ok=False)
        source_path = drawing_dir / str(primary_upload["name"])
        mesh_path = drawing_dir / "mesh.json"

        try:
            for upload in uploads:
                (drawing_dir / str(upload["name"])).write_bytes(bytes(upload["data"]))
            _write_sidecar_alias_if_needed(drawing_dir, primary_upload, uploads)
            model = load_model_file(source_path, fast_step=True)
            mesh_payload = _mesh_payload(model)
            mesh_path.write_text(
                json.dumps(mesh_payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(drawing_dir, ignore_errors=True)
            raise

        source_filename = str(primary_upload["name"])
        now = now_iso()
        record = {
            "id": drawing_id,
            "name": Path(source_filename).stem or source_filename,
            "source_filename": source_filename,
            "attachment_filenames": [
                str(upload["name"])
                for upload in uploads
                if upload is not primary_upload
            ],
            "source_path": str(source_path),
            "mesh_path": str(mesh_path),
            "created": now,
            "updated": now,
            "warnings": list(model.warnings),
            "bounds": _bounds_dict(model.bounds),
            "vertex_count": len(model.vertices),
            "face_count": len(model.faces),
            "web_vertex_count": len(mesh_payload["vertices"]),
            "web_face_count": len(mesh_payload["faces"]),
            "points": [],
            "calibration": None,
        }
        self._drawings.append(record)
        self.save()
        return self._public_record(record, include_points=True)

    def delete_drawing(self, drawing_id: str) -> None:
        record = self._record(drawing_id)
        if record.get("source_kind") == "cad_package":
            raise ValueError("기본 제공 CAD 패키지는 삭제할 수 없습니다.")
        drawing_dir = self.root / drawing_id
        self._drawings.remove(record)
        self.save()
        if drawing_dir.is_dir():
            shutil.rmtree(drawing_dir)

    def _create_cad_package_from_uploads(
        self, files: list[tuple[str, bytes]]
    ) -> dict[str, Any]:
        uploads: dict[str, bytes] = {}
        for filename, data in files:
            safe_name = _safe_filename(filename)
            if safe_name in uploads:
                raise ValueError(f"duplicate upload filename: {safe_name}")
            if not data:
                raise ValueError(f"Uploaded file is empty: {safe_name}")
            uploads[safe_name] = data

        manifest_data = uploads.get("coordinates_manifest.json")
        if manifest_data is None:
            raise ValueError("coordinates_manifest.json is required.")
        try:
            manifest = json.loads(manifest_data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("coordinates_manifest.json is invalid.") from exc
        if not isinstance(manifest, dict):
            raise ValueError("coordinates_manifest.json root must be an object.")

        model_name = next(
            (name for name in uploads if Path(name).suffix.lower() == ".glb"),
            "",
        )
        if not model_name:
            raise ValueError("CAD package requires one GLB drawing file.")

        drawing_id = str(uuid.uuid4())
        drawing_dir = self.root / drawing_id
        drawing_dir.mkdir(parents=True, exist_ok=False)
        try:
            for name, data in uploads.items():
                (drawing_dir / name).write_bytes(data)
            record = self._cad_upload_record(
                drawing_id, drawing_dir, model_name, manifest, uploads
            )
        except Exception:
            shutil.rmtree(drawing_dir, ignore_errors=True)
            raise

        self._drawings.append(record)
        self.save()
        return self._public_record(record, include_points=True)

    def _cad_upload_record(
        self,
        drawing_id: str,
        drawing_dir: Path,
        model_name: str,
        manifest: dict[str, Any],
        uploads: dict[str, bytes],
    ) -> dict[str, Any]:
        rendering = manifest.get("model_rendering") or {}
        model_units_per_mm = (
            rendering.get("model_units_per_mm")
            if isinstance(rendering, dict)
            else None
        )
        if (
            manifest.get("coordinate_frame") != "PRODUCT_CAD_STEP_NATIVE"
            or manifest.get("units") != "mm"
            or not isinstance(model_units_per_mm, (int, float))
            or float(model_units_per_mm) != 0.1
        ):
            raise ValueError(
                "CAD package requires PRODUCT_CAD_STEP_NATIVE coordinates in mm "
                "and model_units_per_mm=0.1."
            )
        points_by_file: dict[str, list[dict[str, Any]]] = {}
        point_sets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group_meta in manifest.get("groups", []):
            if not isinstance(group_meta, dict):
                continue
            point_file = str(group_meta.get("file") or "")
            if not point_file or Path(point_file).name != point_file:
                raise ValueError(f"invalid CAD point filename: {point_file}")
            payload_data = uploads.get(point_file)
            if payload_data is None:
                raise ValueError(f"CAD point file is missing: {point_file}")
            try:
                payload = json.loads(payload_data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid CAD point JSON: {point_file}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"CAD point JSON root must be an object: {point_file}")
            points = _cad_points(manifest, group_meta, payload, [], seen)
            points_by_file[point_file] = points
            point_sets.append(
                {
                    "file": point_file,
                    "group_id": int(group_meta.get("group_id")),
                    "name": str(group_meta.get("name") or point_file),
                    "point_count": len(points),
                    "default_enabled": bool(group_meta.get("default_enabled", False)),
                    "requires_process_review": bool(
                        group_meta.get("requires_process_review", False)
                    ),
                }
            )
        if not point_sets:
            raise ValueError("coordinates_manifest.json has no usable point groups.")

        selected_file = next(
            (item["file"] for item in point_sets if item["default_enabled"]),
            point_sets[0]["file"],
        )
        now = now_iso()
        return {
            "id": drawing_id,
            "source_kind": "uploaded_cad_package",
            "name": str(manifest.get("product_id") or Path(model_name).stem),
            "source_filename": model_name,
            "attachment_filenames": [name for name in uploads if name != model_name],
            "model_path": str((drawing_dir / model_name).resolve()),
            "manifest_path": str((drawing_dir / "coordinates_manifest.json").resolve()),
            "created": now,
            "updated": now,
            "product_id": str(manifest.get("product_id") or ""),
            "coordinate_frame": str(manifest.get("coordinate_frame") or ""),
            "units": str(manifest.get("units") or ""),
            "model_rendering": dict(manifest.get("model_rendering") or {}),
            "point_sets": point_sets,
            "selected_point_file": selected_file,
            "points_by_file": points_by_file,
            "calibration_by_file": {},
            "calibration": None,
            "warnings": [],
        }

    def mesh(self, drawing_id: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        try:
            return json.loads(Path(record["mesh_path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise KeyError(f"drawing mesh is missing: {drawing_id}") from exc

    def model_path(self, drawing_id: str) -> Path:
        record = self._record(drawing_id)
        if record.get("source_kind") not in CAD_PACKAGE_KINDS:
            raise KeyError(f"CAD model is missing: {drawing_id}")
        path = Path(str(record.get("model_path") or ""))
        if not path.is_file():
            raise KeyError(f"CAD model is missing: {drawing_id}")
        return path

    def select_point_set(self, drawing_id: str, point_file: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        point_files = {
            str(item.get("file"))
            for item in record.get("point_sets", [])
            if isinstance(item, dict)
        }
        if point_file not in point_files:
            raise ValueError(f"unknown drawing point file: {point_file}")
        record["selected_point_file"] = point_file
        record["calibration"] = (record.get("calibration_by_file") or {}).get(point_file)
        record["updated"] = now_iso()
        self.save()
        return self._public_record(record, include_points=True)

    def upsert_point(self, drawing_id: str, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(drawing_id)
        points = self._points(record)
        point_id = str(params.get("point_id") or "")
        if point_id:
            point = self._find_point(points, point_id)
        else:
            point = {
                "id": str(uuid.uuid4()),
                "x": 0.0,
                "y": 0.0,
                "z": 0.0,
                "calibration": False,
                "calibration_slot": None,
                "pan": None,
                "tilt": None,
                "label": "",
            }
            points.append(point)

        if "calibration" in params:
            requested_calibration = bool(params["calibration"])
            if requested_calibration and not point.get("calibration"):
                selected = [item for item in points if item.get("calibration") and item is not point]
                if len(selected) >= 10:
                    raise ValueError("캘리브레이션 기준점은 최대 10개까지 선택할 수 있습니다.")
            point["calibration"] = requested_calibration
        for key in ("x", "y", "z", "pan", "tilt"):
            if key in params:
                value = params[key]
                point[key] = None if value in ("", None) else float(value)
        if "label" in params:
            point["label"] = str(params.get("label") or "")
        if point.get("calibration") and point.get("calibration_slot") is None:
            point["calibration_slot"] = sum(1 for item in points if item.get("calibration")) - 1
        if not point.get("calibration"):
            point["calibration_slot"] = None

        updated_points = [_point_payload(item) for item in points]
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            record.setdefault("points_by_file", {})[
                str(record.get("selected_point_file") or "")
            ] = updated_points
        else:
            record["points"] = updated_points
        record["updated"] = now_iso()
        self.save()
        return self._public_point(point, points.index(point) + 1)

    def delete_point(self, drawing_id: str, point_id: str) -> None:
        record = self._record(drawing_id)
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            raise ValueError("CAD 패키지의 원본 도면 포인트는 삭제할 수 없습니다.")
        points = self._points(record)
        record["points"] = [item for item in points if str(item.get("id")) != point_id]
        record["updated"] = now_iso()
        self.save()

    def reorder_points(self, drawing_id: str, ordered_ids: list[str]) -> dict[str, Any]:
        record = self._record(drawing_id)
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            raise ValueError("CAD 패키지의 도면 포인트 순서는 원본 파일 순서를 유지합니다.")
        points = self._points(record)
        by_id = {str(point.get("id")): point for point in points}
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for point_id in ordered_ids:
            point = by_id.get(str(point_id))
            if point is None or str(point_id) in seen:
                continue
            ordered.append(point)
            seen.add(str(point_id))
        ordered.extend(
            point
            for point in points
            if str(point.get("id")) not in seen
        )
        record["points"] = [_point_payload(item) for item in ordered]
        record["updated"] = now_iso()
        self.save()
        return self._public_record(record, include_points=True)

    def calibrate(self, drawing_id: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        points = [DrawingPoint.from_dict(item) for item in self._points(record)]
        taught_count = sum(
            1 for point in points if point.calibration and point.has_pan_tilt
        )
        if taught_count > 10:
            raise CalibrationError("캘리브레이션 기준점은 최대 10개까지 사용할 수 있습니다.")
        fit = fit_affine_calibration(points)
        updated: list[DrawingPoint] = []
        for point in points:
            if not point.calibration:
                point.pan, point.tilt = fit.predict(point)
            updated.append(point)
        metadata_by_id = {str(item.get("id")): item for item in self._points(record)}
        calibrated_points = []
        for point in updated:
            payload = point.to_dict()
            source = metadata_by_id.get(point.id, {})
            for key in CAD_POINT_METADATA:
                if key in source:
                    payload[key] = source[key]
            calibrated_points.append(payload)
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            selected_file = str(record.get("selected_point_file") or "")
            record.setdefault("points_by_file", {})[selected_file] = calibrated_points
        else:
            record["points"] = calibrated_points
        calibration = {
            "updated": now_iso(),
            "points_used": fit.points_used,
            "rms_pan_error": round(fit.rms_pan_error, 4),
            "rms_tilt_error": round(fit.rms_tilt_error, 4),
            "origin": list(fit.origin),
            "scale": fit.scale,
        }
        record["calibration"] = calibration
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            record.setdefault("calibration_by_file", {})[
                str(record.get("selected_point_file") or "")
            ] = calibration
        record["updated"] = now_iso()
        self.save()
        return self._public_record(record, include_points=True)

    def estimate_xy(self, drawing_id: str, pan: float, tilt: float) -> dict[str, Any]:
        record = self._record(drawing_id)
        drawing_points = [DrawingPoint.from_dict(item) for item in self._points(record)]
        fit = fit_inverse_xy_calibration(drawing_points)
        x, y = fit.predict(pan, tilt)
        return {
            "x": x,
            "y": y,
            "z": None,
            "z_status": "unconfirmed",
            "z_reason": "Pan/Tilt→X/Y calibration does not determine Z.",
            "units": "drawing_units",
            "coordinate_frame": f"drawing:{drawing_id}",
            "points_used": fit.points_used,
            "rms_x_error": round(fit.rms_x_error, 4),
            "rms_y_error": round(fit.rms_y_error, 4),
        }

    def estimate_recipe_coordinates(
        self,
        drawing_id: str,
        recipe_store: RecipeStore,
        recipe_id: str,
    ) -> dict[str, Any]:
        recipe = recipe_store.get_recipe(recipe_id)
        estimates = []
        for point in recipe.points:
            estimate = self.estimate_xy(drawing_id, point.pan, point.tilt)
            estimates.append({"point_id": point.id, **estimate})
        return {
            "drawing_id": drawing_id,
            "recipe_id": recipe_id,
            "estimates": estimates,
        }

    def export_to_recipe(self, drawing_id: str, recipe_store: RecipeStore, recipe_id: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        recipe = recipe_store.get_recipe(recipe_id)
        exported = 0
        for index, point in enumerate(self._points(record), start=1):
            if point.get("pan") is None or point.get("tilt") is None:
                continue
            label = str(point.get("label") or f"{record.get('name', 'Drawing')} P{index}")
            target_id = str(point.get("target_id") or point.get("id") or label)
            group_id = point.get("group_id")
            source_key = f"CAD_TARGET {drawing_id} {group_id}:{target_id}"
            note = (
                f"{source_key}\n도면 {record.get('name', '')} / "
                f"X={float(point.get('x', 0.0)):.3f}, "
                f"Y={float(point.get('y', 0.0)):.3f}, "
                f"Z={float(point.get('z', 0.0)):.3f} / "
                f"contact-Z {'인증' if point.get('contact_z_certified') else '미인증'}"
            )
            existing = next(
                (
                    recipe_point
                    for recipe_point in recipe.points
                    if recipe_point.note.splitlines()[:1] == [source_key]
                ),
                None,
            )
            recipe_store.upsert_point(
                recipe_id,
                point_id=existing.id if existing is not None else None,
                name=label,
                pan=float(point["pan"]),
                tilt=float(point["tilt"]),
                note=note,
            )
            exported += 1
        return {
            "exported": exported,
            "recipe": recipe_store.get_recipe(recipe_id).to_dict(),
        }

    def import_from_recipe(self, drawing_id: str, recipe_store: RecipeStore, recipe_id: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        recipe = recipe_store.get_recipe(recipe_id)
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            points = self._points(record)
            by_source_key = {
                f"CAD_TARGET {drawing_id} {point.get('group_id')}:{point.get('target_id') or point.get('id')}": point
                for point in points
            }
            by_label: dict[str, list[dict[str, Any]]] = {}
            for point in points:
                by_label.setdefault(str(point.get("label") or ""), []).append(point)

            imported = 0
            for recipe_point in recipe.points:
                source_key = recipe_point.note.splitlines()[0] if recipe_point.note else ""
                point = by_source_key.get(source_key)
                if point is None:
                    matches = by_label.get(recipe_point.name, [])
                    point = matches[0] if len(matches) == 1 else None
                if point is None:
                    continue
                point["pan"] = float(recipe_point.pan)
                point["tilt"] = float(recipe_point.tilt)
                imported += 1
            if not imported:
                raise ValueError("선택한 포인트 파일과 일치하는 레시피 포인트가 없습니다.")
            record["updated"] = now_iso()
            self.save()
            return {
                "imported": imported,
                "drawing": self.get_drawing(drawing_id),
            }

        estimates = [
            self.estimate_xy(drawing_id, point.pan, point.tilt)
            for point in recipe.points
        ]
        if any(estimate["z"] is None for estimate in estimates):
            raise ValueError(
                "recipe import cannot create drawing points because Z is unconfirmed"
            )
        imported = 0
        for recipe_point, estimate in zip(recipe.points, estimates):
            self.upsert_point(
                drawing_id,
                {
                    "label": recipe_point.name,
                    "x": estimate["x"],
                    "y": estimate["y"],
                    "z": estimate["z"],
                    "pan": recipe_point.pan,
                    "tilt": recipe_point.tilt,
                    "calibration": False,
                },
            )
            imported += 1
        return {
            "imported": imported,
            "drawing": self.get_drawing(drawing_id),
        }

    def _record(self, drawing_id: str) -> dict[str, Any]:
        for record in self._drawings:
            if record.get("id") == drawing_id:
                return record
        raise KeyError(f"unknown drawing: {drawing_id}")

    def _points(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            point_file = str(record.get("selected_point_file") or "")
            points_by_file = record.setdefault("points_by_file", {})
            points = points_by_file.setdefault(point_file, [])
            if not isinstance(points, list):
                points_by_file[point_file] = []
            return points_by_file[point_file]
        points = record.setdefault("points", [])
        if not isinstance(points, list):
            record["points"] = []
        return record["points"]

    def _find_point(self, points: list[dict[str, Any]], point_id: str) -> dict[str, Any]:
        for point in points:
            if str(point.get("id")) == point_id:
                return point
        raise KeyError(f"unknown drawing point: {point_id}")

    def _public_record(self, record: dict[str, Any], *, include_points: bool) -> dict[str, Any]:
        result = {
            "id": str(record.get("id")),
            "name": str(record.get("name") or record.get("source_filename") or "Drawing"),
            "source_filename": str(record.get("source_filename") or ""),
            "attachment_filenames": list(record.get("attachment_filenames") or []),
            "created": str(record.get("created") or ""),
            "updated": str(record.get("updated") or ""),
            "warnings": list(record.get("warnings") or []),
            "bounds": record.get("bounds"),
            "vertex_count": int(record.get("vertex_count") or 0),
            "face_count": int(record.get("face_count") or 0),
            "web_vertex_count": int(record.get("web_vertex_count") or 0),
            "web_face_count": int(record.get("web_face_count") or 0),
            "calibration": record.get("calibration"),
            "point_count": len(record.get("points") or []),
            "deletable": record.get("source_kind") != "cad_package",
        }
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            result.update(
                {
                    "source_kind": str(record.get("source_kind")),
                    "product_id": str(record.get("product_id") or ""),
                    "coordinate_frame": str(record.get("coordinate_frame") or ""),
                    "units": str(record.get("units") or ""),
                    "model_rendering": dict(record.get("model_rendering") or {}),
                    "point_sets": list(record.get("point_sets") or []),
                    "selected_point_file": str(record.get("selected_point_file") or ""),
                    "point_count": len(self._points(record)),
                    "model_url": f"/api/drawings/{record.get('id')}/model",
                }
            )
        if include_points:
            result["points"] = [
                self._public_point(point, index)
                for index, point in enumerate(self._points(record), start=1)
            ]
        return result

    def _public_point(self, point: dict[str, Any], order: int) -> dict[str, Any]:
        payload = _point_payload(point)
        payload["order"] = order
        if not payload.get("label"):
            payload["label"] = f"P{order}"
        return payload

    def _sync_cad_packages(self) -> None:
        if self.package_root is None or not self.package_root.is_dir():
            return

        previous = {
            str(record.get("package_name")): record
            for record in self._drawings
            if record.get("source_kind") == "cad_package"
        }
        legacy = [
            record
            for record in self._drawings
            if record.get("source_kind") != "cad_package"
        ]
        packages: list[dict[str, Any]] = []
        for package_dir in sorted(self.package_root.iterdir(), key=lambda item: item.name.casefold()):
            if not package_dir.is_dir():
                continue
            manifest_path = package_dir / "coordinates" / "coordinates_manifest.json"
            model_path = package_dir / "model" / "product.glb"
            if not manifest_path.is_file() or not model_path.is_file():
                continue
            manifest = _json_object(manifest_path)
            package_name = package_dir.name
            old = previous.get(package_name, {})
            points_by_file: dict[str, list[dict[str, Any]]] = {}
            point_sets: list[dict[str, Any]] = []
            old_points_by_file = old.get("points_by_file") or {}
            seen: set[str] = set()
            for group_meta in manifest.get("groups", []):
                if not isinstance(group_meta, dict):
                    continue
                point_file = str(group_meta.get("file") or "")
                if not point_file or Path(point_file).name != point_file:
                    raise ValueError(f"invalid CAD point filename: {point_file}")
                payload = _json_object(package_dir / "coordinates" / point_file)
                points = _cad_points(
                    manifest,
                    group_meta,
                    payload,
                    old_points_by_file.get(point_file, []),
                    seen,
                )
                points_by_file[point_file] = points
                point_sets.append(
                    {
                        "file": point_file,
                        "group_id": int(group_meta.get("group_id")),
                        "name": str(group_meta.get("name") or point_file),
                        "point_count": len(points),
                        "default_enabled": bool(group_meta.get("default_enabled", False)),
                        "requires_process_review": bool(
                            group_meta.get("requires_process_review", False)
                        ),
                    }
                )
            if not point_sets:
                continue
            available_files = {item["file"] for item in point_sets}
            selected_file = str(old.get("selected_point_file") or "")
            if selected_file not in available_files:
                selected_file = next(
                    (
                        item["file"]
                        for item in point_sets
                        if item["default_enabled"]
                    ),
                    point_sets[0]["file"],
                )
            calibration_by_file = dict(old.get("calibration_by_file") or {})
            packages.append(
                {
                    "id": str(old.get("id") or f"cad-{_safe_package_id(package_name)}"),
                    "source_kind": "cad_package",
                    "package_name": package_name,
                    "name": package_name,
                    "source_filename": "product.glb",
                    "model_path": str(model_path.resolve()),
                    "manifest_path": str(manifest_path.resolve()),
                    "created": str(old.get("created") or now_iso()),
                    "updated": str(old.get("updated") or now_iso()),
                    "product_id": str(manifest.get("product_id") or ""),
                    "coordinate_frame": str(manifest.get("coordinate_frame") or ""),
                    "units": str(manifest.get("units") or ""),
                    "model_rendering": dict(manifest.get("model_rendering") or {}),
                    "point_sets": point_sets,
                    "selected_point_file": selected_file,
                    "points_by_file": points_by_file,
                    "calibration_by_file": calibration_by_file,
                    "calibration": calibration_by_file.get(selected_file),
                    "warnings": [],
                }
            )
        self._drawings = legacy + packages


def _json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid CAD JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"CAD JSON root must be an object: {path}")
    return payload


def _cad_points(
    manifest: dict[str, Any],
    group_meta: dict[str, Any],
    payload: dict[str, Any],
    previous: Any,
    seen: set[str],
) -> list[dict[str, Any]]:
    group = payload.get("group") if isinstance(payload.get("group"), dict) else {}
    group_id = int(group_meta.get("group_id"))
    if (
        payload.get("product_id") != manifest.get("product_id")
        or payload.get("coordinate_frame") != manifest.get("coordinate_frame")
        or payload.get("units") != manifest.get("units")
        or int(group.get("group_id", -1)) != group_id
        or not isinstance(payload.get("points"), list)
    ):
        raise ValueError(f"CAD coordinate contract mismatch: group {group_id}")
    previous_by_id = {
        str(point.get("id")): point
        for point in previous
        if isinstance(point, dict)
    }
    result: list[dict[str, Any]] = []
    for source in payload["points"]:
        if not isinstance(source, dict):
            raise ValueError(f"invalid CAD point: group {group_id}")
        target_id = str(source.get("target_id") or "")
        point_group_id = int(source.get("group_id", -1))
        xyz = source.get("xyz_mm")
        key = f"{point_group_id}:{target_id}"
        if (
            point_group_id != group_id
            or not target_id
            or not isinstance(xyz, list)
            or len(xyz) != 3
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in xyz)
            or key in seen
        ):
            raise ValueError(f"invalid or duplicate CAD target: {key}")
        seen.add(key)
        old = previous_by_id.get(key, {})
        point = {
            "id": key,
            "label": target_id,
            "x": float(xyz[0]),
            "y": float(xyz[1]),
            "z": float(xyz[2]),
            "pan": old.get("pan"),
            "tilt": old.get("tilt"),
            "calibration": bool(old.get("calibration", False)),
            "calibration_slot": old.get("calibration_slot"),
        }
        for metadata_key in CAD_POINT_METADATA:
            if metadata_key in source:
                point[metadata_key] = source[metadata_key]
        result.append(point)
    return result


def _safe_package_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9가-힣_.-]+", "-", name).strip("-.")
    return safe or str(uuid.uuid4())


def _safe_filename(filename: str) -> str:
    stem = Path((filename or "drawing").replace("\\", "/")).name
    safe = re.sub(r"[^A-Za-z0-9가-힣_. -]+", "_", stem).strip(" .")
    return safe or "drawing.stl"


def _unique_filename(filename: str, used_names: set[str]) -> str:
    path = Path(filename)
    stem = path.stem or "drawing"
    suffix = path.suffix
    candidate = filename
    index = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used_names.add(candidate.casefold())
    return candidate


def _write_sidecar_alias_if_needed(
    drawing_dir: Path,
    primary_upload: dict[str, Any],
    uploads: list[dict[str, Any]],
) -> None:
    if primary_upload["suffix"] not in STEP_SOURCE_EXTENSIONS:
        return

    primary_stem = Path(str(primary_upload["name"])).stem.casefold()
    mesh_uploads = [
        upload
        for upload in uploads
        if (
            upload is not primary_upload and
            upload["suffix"] in SIDE_CAR_MESH_EXTENSIONS
        )
    ]
    if not mesh_uploads:
        return
    if any(Path(str(upload["name"])).stem.casefold() == primary_stem for upload in mesh_uploads):
        return
    if len(mesh_uploads) != 1:
        return

    sidecar = mesh_uploads[0]
    alias_path = drawing_dir / f"{Path(str(primary_upload['name'])).stem}{sidecar['suffix']}"
    if not alias_path.exists():
        alias_path.write_bytes(bytes(sidecar["data"]))


def _point_payload(point: dict[str, Any]) -> dict[str, Any]:
    payload = DrawingPoint.from_dict(point).to_dict()
    for key in CAD_POINT_METADATA:
        if key in point:
            payload[key] = point[key]
    return payload


def _mesh_payload(model: MeshModel) -> dict[str, Any]:
    faces = list(model.faces)
    colors = list(model.face_colors)
    sampled = False
    source_suffix = Path(str(model.source_path)).suffix.lower()
    step_preview = (
        source_suffix in {".stp", ".step", ".cad"} and
        not colors
    )
    if len(faces) > MAX_WEB_FACES:
        step = math.ceil(len(faces) / MAX_WEB_FACES)
        faces = faces[::step]
        colors = colors[::step] if len(colors) == len(model.faces) else []
        sampled = True

    vertices = list(model.vertices)
    if faces:
        vertices, faces, colors = _compact_mesh(vertices, faces, colors)
    elif len(vertices) > MAX_WEB_VERTICES:
        step = math.ceil(len(vertices) / MAX_WEB_VERTICES)
        vertices = vertices[::step]
        sampled = True

    return {
        "version": 1,
        "source": model.name,
        "source_extension": source_suffix,
        "step_preview": step_preview,
        "vertices": [[round(x, 6), round(y, 6), round(z, 6)] for x, y, z in vertices],
        "faces": [[a, b, c] for a, b, c in faces],
        "face_colors": [[r, g, b] for r, g, b in colors] if colors else [],
        "bounds": _bounds_dict(_vertex_bounds(vertices)),
        "sampled": sampled,
        "warnings": list(model.warnings),
    }


def _compact_mesh(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    colors: list[tuple[int, int, int]],
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    remap: dict[int, int] = {}
    compact_vertices: list[tuple[float, float, float]] = []
    compact_faces: list[tuple[int, int, int]] = []
    compact_colors: list[tuple[int, int, int]] = []
    for index, face in enumerate(faces):
        mapped = []
        for vertex_index in face:
            next_index = remap.get(vertex_index)
            if next_index is None:
                next_index = len(compact_vertices)
                remap[vertex_index] = next_index
                compact_vertices.append(vertices[vertex_index])
            mapped.append(next_index)
        if len(set(mapped)) == 3:
            compact_faces.append((mapped[0], mapped[1], mapped[2]))
            if index < len(colors):
                compact_colors.append(colors[index])
    return compact_vertices, compact_faces, compact_colors


def _vertex_bounds(vertices: list[tuple[float, float, float]]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not vertices:
        zero = (0.0, 0.0, 0.0)
        return zero, zero
    xs = [vertex[0] for vertex in vertices]
    ys = [vertex[1] for vertex in vertices]
    zs = [vertex[2] for vertex in vertices]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _bounds_dict(bounds: tuple[tuple[float, float, float], tuple[float, float, float]]) -> dict[str, list[float]]:
    minimum, maximum = bounds
    return {
        "min": [round(value, 6) for value in minimum],
        "max": [round(value, 6) for value in maximum],
    }

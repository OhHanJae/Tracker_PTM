"""Server-side drawing upload, mesh cache, and calibration workflow storage."""

from __future__ import annotations

import json
import io
import math
import re
import shutil
import uuid
import zipfile
from copy import deepcopy
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any

from .drawing_calibration import (
    CalibrationError,
    DrawingPoint,
    MeshModel,
    fit_affine_calibration,
    fit_inverse_xy_calibration,
    load_model_file,
    predict_product_pose,
)
from .recipe_store import RecipePoint, RecipeStore, app_state_dir, now_iso

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
    "enabled",
    # 최초 0°/0° 티칭값. 제품 자세를 여러 번 변경해도 이 기준값은 유지한다.
    "calibration_pan",
    "calibration_tilt",
)


def _drawing_edit(method):
    """Keep failed teaching/calculation changes out of the live stores."""
    @wraps(method)
    def edit(self, *args, **kwargs):
        drawings = deepcopy(self._drawings)
        recipes = deepcopy(self.recipe_store._recipes) if self.recipe_store is not None else None
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self._drawings = drawings
            if recipes is not None:
                self.recipe_store._recipes = recipes
            raise
    return edit


class DrawingStore:
    """JSON-backed drawing workflow store.

    The original upload and the generated lightweight mesh are kept on disk.
    Points stay separate from motion recipes until an explicit import/export.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        package_root: str | Path | None = None,
        recipe_store: RecipeStore | None = None,
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
        self.recipe_store = recipe_store
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
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.recipe_store is None:
            temporary.replace(self.manifest_path)
            return
        previous = self.recipe_store.path.read_bytes() if self.recipe_store.path.exists() else None
        self.recipe_store.save()
        try:
            temporary.replace(self.manifest_path)
        except OSError:
            if previous is None:
                self.recipe_store.path.unlink(missing_ok=True)
            else:
                rollback = self.recipe_store.path.with_suffix(".rollback")
                rollback.write_bytes(previous)
                rollback.replace(self.recipe_store.path)
            raise

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
        if len(files) == 1 and Path(files[0][0]).suffix.lower() == ".zip":
            return self._create_from_archive(files[0][1])
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
            if record.get("source_kind") in CAD_PACKAGE_KINDS:
                model = self.model_path(drawing_id)
                cache = self.root / str(record["id"]) / "native_mesh.json"
                if not cache.is_file() or cache.stat().st_mtime < model.stat().st_mtime:
                    payload = _mesh_payload(load_model_file(model))
                    payload["model_units_per_mm"] = float(
                        (record.get("model_rendering") or {}).get("model_units_per_mm", 0.1)
                    )
                    payload["coordinate_frame"] = record.get("coordinate_frame")
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cache.with_suffix(f".{uuid.uuid4().hex}.tmp")
                    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(cache)
                return json.loads(cache.read_text(encoding="utf-8"))
            return json.loads(Path(record["mesh_path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise KeyError(f"drawing mesh is missing: {drawing_id}") from exc

    def archive(self, drawing_id: str) -> bytes:
        """Export original assets and taught state without exposing server paths."""
        record = self._record(drawing_id)
        files: dict[str, Path] = {}
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            files["product.glb"] = self.model_path(drawing_id)
            manifest = Path(str(record["manifest_path"]))
            files["coordinates_manifest.json"] = manifest
            for group in record.get("point_sets", []):
                name = str(group.get("file") or "")
                if not name or Path(name).name != name:
                    raise ValueError("invalid CAD point filename")
                files[name] = manifest.parent / name
        else:
            folder = self.root / drawing_id
            for name in [record.get("source_filename"), *record.get("attachment_filenames", [])]:
                if name and Path(str(name)).name == name:
                    files[str(name)] = folder / str(name)
        state = self.get_drawing(drawing_id)
        state["backup_schema"] = "pt503.drawing.backup"
        state["backup_version"] = 1
        state["points_by_file"] = record.get("points_by_file", {})
        state["calibration_by_file"] = record.get("calibration_by_file", {})
        if self.recipe_store is not None:
            document = self.recipe_store.export_document()
            document["recipes"] = [recipe for recipe in document["recipes"]
                                   if recipe.get("drawing", {}).get("drawing_id") == drawing_id]
            if document["recipes"]:
                state["recipe_document"] = document
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            if record.get("fvp_archive_path"):
                with zipfile.ZipFile(record["fvp_archive_path"]) as original:
                    for entry in original.infolist():
                        if entry.filename != "monitor_drawing_state.json":
                            archive.writestr(entry.filename, original.read(entry))
            else:
                for name, path in files.items():
                    archive.write(path, name)
            archive.writestr("monitor_drawing_state.json", json.dumps(state, ensure_ascii=False, indent=2))
        return output.getvalue()

    def _create_from_archive(self, data: bytes) -> dict[str, Any]:
        """Restore a bounded flat backup; never extract archive paths to disk."""
        files: list[tuple[str, bytes]] = []
        state: dict[str, Any] | None = None
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 200 or sum(entry.file_size for entry in entries) > 128 * 1024 * 1024:
                raise ValueError("drawing archive exceeds 200 files or 128MB")
            names: set[str] = set()
            for entry in entries:
                name = entry.filename
                if entry.is_dir():
                    continue
                if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name or ":" in name or name in {".", ".."} or name in names:
                    raise ValueError("drawing archive contains an invalid or duplicate path")
                names.add(name)
                payload = archive.read(entry)
                if name == "monitor_drawing_state.json":
                    state = json.loads(payload.decode("utf-8"))
                else:
                    files.append((name, payload))
        if state is not None and (not isinstance(state, dict) or state.get("backup_schema") != "pt503.drawing.backup" or state.get("backup_version") != 1):
            raise ValueError("unsupported drawing backup document")
        if any(Path(name).suffix.lower() == ".zip" for name, _ in files):
            raise ValueError("nested drawing archives are not supported")
        product_manifests = [(name, payload) for name, payload in files
                             if name.startswith("settings/product_library/") and name.endswith("/manifest.json")]
        if product_manifests:
            if len(product_manifests) != 1:
                raise ValueError("제품이 여러 개 들어 있습니다. 제품별 ZIP으로 등록하세요.")
            drawing = self._create_fvp_package(data, dict(files), product_manifests[0][0], create_recipes=state is None)
        else:
            if any(Path(name).name != name for name, _ in files):
                raise ValueError("drawing backup requires flat asset filenames")
            drawing = self.create_from_uploads(files)
        try:
            if state is not None:
                self._restore_backup_state(drawing["id"], state)
        except Exception:
            self.delete_drawing(drawing["id"])
            raise
        return self.get_drawing(drawing["id"])

    def _create_fvp_package(self, data: bytes, files: dict[str, bytes], manifest_name: str,
                            *, create_recipes: bool) -> dict[str, Any]:
        source = json.loads(files[manifest_name].decode("utf-8-sig"))
        if source.get("coordinate_units") != "mm" or source.get("model_units") != "cm":
            raise ValueError("FVP 제품 좌표는 mm, 모델 단위는 cm여야 합니다.")
        model_name = str(PurePosixPath(manifest_name).parent / "model.glb")
        if model_name not in files:
            raise ValueError("FVP 제품 model.glb가 없습니다.")
        product_id = str(source.get("product_id") or "")
        manifest = {"product_id": product_id, "coordinate_frame": "PRODUCT_CAD_STEP_NATIVE",
                    "units": "mm", "model_rendering": {"model_units_per_mm": 0.1}, "groups": []}
        groups: dict[int, dict[str, Any]] = {}
        for recipe in source.get("recipes", []):
            for point in recipe.get("points", []):
                group_id = int(point["group_id"])
                group = groups.setdefault(group_id, {"name": recipe.get("name") or f"그룹 {group_id}", "points": {}})
                target = str(point.get("target_id") or point.get("source_cad_target_id") or point.get("name") or point["point_id"])
                converted = {**point, "target_id": target}
                previous = group["points"].get(target)
                if previous is not None and previous.get("xyz_mm") != converted.get("xyz_mm"):
                    raise ValueError(f"FVP 레시피 사이 CAD 좌표가 다릅니다: {target}")
                group["points"][target] = converted
        uploads = [("product.glb", files[model_name])]
        for group_id, group in groups.items():
            name = f"group_{group_id}.json"
            manifest["groups"].append({"group_id": group_id, "name": group["name"], "file": name,
                                       "default_enabled": group_id != 3})
            payload = {**manifest, "group": {"group_id": group_id}, "points": list(group["points"].values())}
            uploads.append((name, json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        uploads.append(("coordinates_manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8")))
        drawing = self._create_cad_package_from_uploads(uploads)
        record = self._record(drawing["id"])
        try:
            original = self.root / drawing["id"] / "fvp_source.zip"
            original.write_bytes(data)
            record["fvp_archive_path"] = str(original)
            record["name"] = str(source.get("name") or product_id)
            if create_recipes and self.recipe_store is not None:
                document = {"schema": "pt503.recipe.bundle", "version": 1, "recipes": []}
                for item in source.get("recipes", []):
                    group_ids = {int(point["group_id"]) for point in item.get("points", [])}
                    if len(group_ids) != 1:
                        raise ValueError("FVP 레시피는 하나의 그룹에 속해야 합니다.")
                    point_file = f"group_{next(iter(group_ids))}.json"
                    targets = {str(point.get("target_id") or point.get("source_cad_target_id") or point.get("name") or point["point_id"]) for point in item["points"]}
                    points = [deepcopy(point) for point in record["points_by_file"][point_file]
                              if point["target_id"] in targets]
                    document["recipes"].append({"id": str(uuid.uuid4()), "name": str(item.get("name") or "Recipe"),
                                                "points": [], "drawing": self._new_recipe_state(record, point_file, points)})
                self.recipe_store.import_document(document)
            self.save()
        except Exception:
            self.delete_drawing(drawing["id"])
            raise
        return self.get_drawing(drawing["id"])

    def _restore_backup_state(self, drawing_id: str, state: dict[str, Any]) -> None:
        record = self._record(drawing_id)

        def checked_points(value: Any, original: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                raise ValueError("backup points must be a list")
            source = {str(point["id"]): point for point in original or []}
            result: list[dict[str, Any]] = []
            seen: set[str] = set()
            for point in value:
                if not isinstance(point, dict) or not point.get("id") or str(point["id"]) in seen:
                    raise ValueError("invalid or duplicate drawing point")
                point_id = str(point["id"])
                seen.add(point_id)
                if original is not None and point_id not in source:
                    raise ValueError("backup point does not match CAD package")
                for key in ("x", "y", "z", "pan", "tilt", "calibration_pan", "calibration_tilt"):
                    number = point.get(key)
                    if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)):
                        raise ValueError(f"invalid backup coordinate: {key}")
                if point.get("pan") is not None and not 0 <= point["pan"] < 360:
                    raise ValueError("backup Pan is out of range")
                if point.get("tilt") is not None and not -60 <= point["tilt"] <= 60:
                    raise ValueError("backup Tilt is out of range")
                if original is not None:
                    raw = source[point_id]
                    if any(point.get(key) != raw.get(key) for key in ("x", "y", "z")):
                        raise ValueError("backup changes original CAD coordinates")
                    restored = dict(raw)
                    for key in ("pan", "tilt", "calibration", "calibration_slot", "calibration_pan", "calibration_tilt", "label"):
                        if key in point:
                            restored[key] = point[key]
                else:
                    restored = dict(point)
                result.append(_point_payload(restored))
            if original is not None and seen != set(source):
                raise ValueError("backup CAD points are incomplete")
            return result

        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            if state.get("coordinate_frame") != record.get("coordinate_frame") or state.get("units") != record.get("units"):
                raise ValueError("backup coordinate frame does not match")
            groups = state.get("points_by_file")
            if not isinstance(groups, dict) or set(groups) != set(record["points_by_file"]):
                raise ValueError("backup CAD groups do not match")
            restored_groups = {
                name: checked_points(points, record["points_by_file"][name]) for name, points in groups.items()
            }
            selected = str(state.get("selected_point_file") or "")
            if selected not in restored_groups:
                raise ValueError("backup selected group is invalid")
            record["points_by_file"] = restored_groups
            record["selected_point_file"] = selected
            record["calibration_by_file"] = dict(state.get("calibration_by_file") or {})
        else:
            record["points"] = checked_points(state.get("points"))
        for key in ("product_rotation_deg", "product_tilt_deg"):
            number = state.get(key, 0.0)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or abs(number) > 360:
                raise ValueError("invalid product pose in drawing backup")
            record[key] = float(number)
        calibration = state.get("calibration")
        if calibration is not None and not isinstance(calibration, dict):
            raise ValueError("invalid backup calibration")
        record["calibration"] = calibration
        if state.get("recipe_document") is not None:
            if self.recipe_store is None:
                raise ValueError("레시피 저장소가 필요합니다.")
            document = deepcopy(state["recipe_document"])
            recipes = RecipeStore._validated_document(document)
            existing_recipe_ids = {recipe.id for recipe in self.recipe_store._recipes}
            existing_point_ids = {point.id for recipe in self.recipe_store._recipes for point in recipe.points}
            remapped = {recipe.id: str(uuid.uuid4()) for recipe in recipes if recipe.id in existing_recipe_ids}
            for recipe, payload in zip(recipes, document["recipes"]):
                context = recipe.drawing
                point_file = context.get("point_file")
                originals = record.get("points_by_file", {}).get(point_file)
                if context.get("product_id") != record.get("product_id") or originals is None:
                    raise ValueError("backup recipe does not match CAD package")
                checked_points(context["points"], [point for point in originals
                                                  if point["id"] in {p["id"] for p in context["points"]}])
                payload["drawing"]["drawing_id"] = drawing_id
                payload["id"] = remapped.get(recipe.id, recipe.id)
                payload["source_recipe_id"] = remapped.get(payload.get("source_recipe_id"), payload.get("source_recipe_id", ""))
                for point in payload["points"]:
                    if point["id"] in existing_point_ids:
                        point["id"] = str(uuid.uuid4())
            self.recipe_store.import_document(document)
            selected = str(state.get("selected_recipe_id") or "")
            record["selected_recipe_id"] = remapped.get(selected, selected) if any(recipe.id == selected for recipe in recipes) else ""
        record["updated"] = now_iso()
        self.save()

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
        record["selected_recipe_id"] = ""
        record["calibration"] = None if self.recipe_store is not None else (record.get("calibration_by_file") or {}).get(point_file)
        record["product_rotation_deg"] = 0.0
        record["product_tilt_deg"] = 0.0
        record["updated"] = now_iso()
        self.save()
        return self._public_record(record, include_points=True)

    @staticmethod
    def _new_recipe_state(record: dict[str, Any], point_file: str,
                          points: list[dict[str, Any]]) -> dict[str, Any]:
        return {"drawing_id": record["id"], "product_id": record.get("product_id") or record["id"],
                "point_file": point_file, "points": deepcopy(points), "calibration": None,
                "product_rotation_deg": 0.0, "product_tilt_deg": 0.0}

    @_drawing_edit
    def select_recipe(self, drawing_id: str, recipe_id: str) -> dict[str, Any]:
        if self.recipe_store is None:
            raise ValueError("레시피 저장소가 필요합니다.")
        record = self._record(drawing_id)
        recipe = self.recipe_store.get_recipe(recipe_id)
        point_file = str(record.get("selected_point_file") or "")
        if not point_file:
            raise ValueError("CAD 그룹을 먼저 선택하세요.")
        if recipe.drawing:
            if (recipe.drawing.get("product_id") != record.get("product_id")
                    or recipe.drawing.get("point_file") != point_file):
                raise ValueError("선택한 레시피는 다른 도면/그룹에 연결되어 있습니다.")
            originals = {point["id"]: point for point in record["points_by_file"][point_file]}
            if any(point["id"] not in originals or any(point[key] != originals[point["id"]][key]
                   for key in ("x", "y", "z")) for point in recipe.drawing["points"]):
                raise ValueError("레시피 좌표가 원본 CAD 그룹과 다릅니다.")
            recipe.drawing["drawing_id"] = drawing_id
        else:
            points = deepcopy(record["points_by_file"][point_file])
            for point in points:
                point.update(pan=None, tilt=None, calibration=False, calibration_slot=None)
                point.pop("calibration_pan", None)
                point.pop("calibration_tilt", None)
            recipe.drawing = self._new_recipe_state(record, point_file, points)
            # 기존 레시피는 이름이 일치하는 도면 포인트의 티칭값만 가져온다.
            by_name = {point.name: point for point in recipe.points}
            by_target = {}
            for point in recipe.points:
                source_key = point.note.splitlines()[0] if point.note else ""
                target = point.drawing_point_id or (source_key.rsplit(" ", 1)[-1] if source_key.startswith("CAD_TARGET ") else "")
                if target:
                    by_target[target] = point
            imported = 0
            for point in recipe.drawing["points"]:
                taught = by_target.get(point["id"]) or by_name.get(point.get("label"))
                if taught is not None:
                    point.update(pan=taught.pan, tilt=taught.tilt)
                    imported += 1
            if recipe.points and not imported:
                raise ValueError("선택한 그룹과 일치하는 레시피 포인트가 없습니다.")
        record["selected_recipe_id"] = recipe.id
        self.save()
        return self.get_drawing(drawing_id)

    def _context(self, record: dict[str, Any]) -> dict[str, Any]:
        recipe_id = record.get("selected_recipe_id")
        if recipe_id and self.recipe_store is not None:
            try:
                context = self.recipe_store.get_recipe(recipe_id).drawing
            except KeyError:
                context = {}
            if (context.get("drawing_id") == record["id"]
                    and context.get("point_file") == record.get("selected_point_file")):
                return context
        return record

    def _set_points(self, record: dict[str, Any], points: list[dict[str, Any]]) -> None:
        context = self._context(record)
        if context is not record:
            context["points"] = points
        elif record.get("source_kind") in CAD_PACKAGE_KINDS:
            record.setdefault("points_by_file", {})[record["selected_point_file"]] = points
        else:
            record["points"] = points

    @_drawing_edit
    def upsert_point(self, drawing_id: str, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(drawing_id)
        points = deepcopy(self._points(record))
        point_id = str(params.get("point_id") or "")
        if point_id:
            point = self._find_point(points, point_id)
        else:
            if record.get("source_kind") in CAD_PACKAGE_KINDS:
                raise ValueError("원본 CAD 그룹에 포인트를 추가할 수 없습니다.")
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

        context = self._context(record)
        if any(key in params for key in ("pan", "tilt", "calibration")) and (
                context.get("product_rotation_deg", 0) or context.get("product_tilt_deg", 0)):
            raise ValueError("기준점 재티칭은 제품 0°/0°에서 수행하세요.")
        if "calibration" in params:
            requested_calibration = bool(params["calibration"])
            point["calibration"] = requested_calibration
        for key in ("x", "y", "z", "pan", "tilt"):
            if key in params:
                value = params[key]
                if record.get("source_kind") in CAD_PACKAGE_KINDS and key in ("x", "y", "z") and value != point.get(key):
                    raise ValueError("원본 CAD 좌표는 수정할 수 없습니다.")
                if value not in ("", None) and not math.isfinite(float(value)):
                    raise ValueError(f"{key} must be finite")
                point[key] = None if value in ("", None) else float(value)
        if point.get("pan") is not None:
            point["pan"] %= 360.0
        if point.get("tilt") is not None and not -60 <= point["tilt"] <= 60:
            raise ValueError("Tilt must be within -60..60")
        if ("pan" in params or "tilt" in params) and point.get("calibration"):
            point.pop("calibration_pan", None)
            point.pop("calibration_tilt", None)
        if "label" in params:
            point["label"] = str(params.get("label") or "")
        if point.get("calibration") and point.get("calibration_slot") is None:
            point["calibration_slot"] = sum(1 for item in points if item.get("calibration")) - 1
        if not point.get("calibration"):
            point["calibration_slot"] = None
            point.pop("calibration_pan", None)
            point.pop("calibration_tilt", None)

        updated_points = [_point_payload(item) for item in points]
        self._set_points(record, updated_points)
        self._context(record)["calibration"] = None
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

    @staticmethod
    def _pose_axis_name(axis: int) -> str:
        return ("X", "Y", "Z")[axis]

    def _all_product_points(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            groups = record.get("points_by_file") or {}
            return [
                point
                for points in groups.values()
                if isinstance(points, list)
                for point in points
                if isinstance(point, dict)
            ]
        return [point for point in self._points(record) if isinstance(point, dict)]

    def _product_pose_frame(
        self, record: dict[str, Any]
    ) -> tuple[tuple[float, float, float], int, int, tuple[float, float, float]]:
        points = self._all_product_points(record)
        if not points:
            raise CalibrationError("제품 자세를 계산할 도면 좌표가 없습니다.")

        coordinates = [
            (float(point.get("x", 0.0)), float(point.get("y", 0.0)), float(point.get("z", 0.0)))
            for point in points
        ]
        minimums = tuple(min(position[axis] for position in coordinates) for axis in range(3))
        maximums = tuple(max(position[axis] for position in coordinates) for axis in range(3))
        center = tuple((minimums[axis] + maximums[axis]) / 2.0 for axis in range(3))
        spans = tuple(maximums[axis] - minimums[axis] for axis in range(3))
        axes = sorted(range(3), key=lambda axis: (-spans[axis], axis))
        return center, axes[0], axes[1], spans

    @staticmethod
    def _rotate_product_position(
        position: tuple[float, float, float],
        center: tuple[float, float, float],
        axis: int,
        angle_deg: float,
    ) -> tuple[float, float, float]:
        if abs(angle_deg) < 1e-12:
            return position

        x = position[0] - center[0]
        y = position[1] - center[1]
        z = position[2] - center[2]
        radians = math.radians(angle_deg)
        cosine = math.cos(radians)
        sine = math.sin(radians)

        # 오른손 좌표계 기준 양(+)의 회전.
        if axis == 0:      # X
            y, z = y * cosine - z * sine, y * sine + z * cosine
        elif axis == 1:    # Y
            x, z = x * cosine + z * sine, -x * sine + z * cosine
        else:              # Z
            x, y = x * cosine - y * sine, x * sine + y * cosine

        return center[0] + x, center[1] + y, center[2] + z

    def _posed_product_position(
        self,
        record: dict[str, Any],
        position: tuple[float, float, float],
        product_rotation_deg: float,
        product_tilt_deg: float,
    ) -> tuple[tuple[float, float, float], dict[str, Any]]:
        center, rotation_axis, tilt_axis, spans = self._product_pose_frame(record)
        rotated = self._rotate_product_position(
            position, center, rotation_axis, product_rotation_deg
        )
        posed = self._rotate_product_position(
            rotated, center, tilt_axis, product_tilt_deg
        )
        return posed, {
            "center": center,
            "rotation_axis": rotation_axis,
            "tilt_axis": tilt_axis,
            "spans": spans,
        }

    def _reference_calibration_points(
        self,
        record: dict[str, Any],
        *,
        capture_zero_pose: bool,
    ) -> list[DrawingPoint]:
        raw_points = self._points(record)
        previous_calibration = self._context(record).get("calibration") or {}
        previous_rotation = float(previous_calibration.get("product_rotation_deg", 0.0) or 0.0)
        previous_tilt = float(previous_calibration.get("product_tilt_deg", 0.0) or 0.0)
        previous_is_zero = abs(previous_rotation) < 1e-9 and abs(previous_tilt) < 1e-9

        reference_points: list[DrawingPoint] = []
        for raw in raw_points:
            point = DrawingPoint.from_dict(raw)
            if not point.calibration:
                reference_points.append(point)
                continue

            if not point.has_pan_tilt:
                reference_points.append(point)
                continue

            baseline_missing = (
                raw.get("calibration_pan") is None
                or raw.get("calibration_tilt") is None
            )
            if capture_zero_pose and (baseline_missing or previous_is_zero):
                # 최초 0°/0° 캘리브레이션 또는 0°/0°에서 명시적으로 재티칭한 경우만
                # 기준 Pan/Tilt를 갱신한다. 30° -> 0°처럼 자세만 되돌릴 때는
                # 직전 자세에서 계산된 Pan/Tilt가 기준값을 덮어쓰지 않게 한다.
                raw["calibration_pan"] = float(point.pan)
                raw["calibration_tilt"] = float(point.tilt)
            elif baseline_missing:
                # 기존 버전에서 이미 0°/0° 캘리브레이션한 데이터는 자동 마이그레이션한다.
                if previous_is_zero:
                    raw["calibration_pan"] = float(point.pan)
                    raw["calibration_tilt"] = float(point.tilt)
                else:
                    raise CalibrationError(
                        "제품 각도 보정 기준값이 없습니다. 제품을 0°/0°로 놓고 기준점을 다시 캘리브레이션하세요."
                    )

            payload = point.to_dict()
            payload["pan"] = raw.get("calibration_pan")
            payload["tilt"] = raw.get("calibration_tilt")
            reference_points.append(DrawingPoint.from_dict(payload))

        return reference_points

    @_drawing_edit
    def calibrate(
        self,
        drawing_id: str,
        *,
        product_rotation_deg: float = 0.0,
        product_tilt_deg: float = 0.0,
    ) -> dict[str, Any]:
        record = self._record(drawing_id)
        product_rotation_deg = float(product_rotation_deg)
        product_tilt_deg = float(product_tilt_deg)
        if (
            not math.isfinite(product_rotation_deg)
            or not math.isfinite(product_tilt_deg)
            or abs(product_rotation_deg) > 360.0
            or abs(product_tilt_deg) > 360.0
        ):
            raise ValueError("제품 회전/틸팅 각도는 -360..360도 범위여야 합니다.")

        zero_pose = (
            abs(product_rotation_deg) < 1e-9
            and abs(product_tilt_deg) < 1e-9
        )
        reference_points = self._reference_calibration_points(
            record,
            capture_zero_pose=zero_pose,
        )
        fit = fit_affine_calibration(reference_points)
        reference_by_id = {point.id: point for point in reference_points}
        raw_points = self._points(record)

        center, rotation_axis, tilt_axis, spans = self._product_pose_frame(record)
        calibrated_points: list[dict[str, Any]] = []
        for raw in raw_points:
            point = DrawingPoint.from_dict(raw)
            reference = reference_by_id.get(point.id, point)

            if zero_pose and point.calibration and reference.has_pan_tilt:
                pan = float(reference.pan)
                tilt = float(reference.tilt)
            else:
                if zero_pose:
                    pan, tilt = fit.predict(point)
                else:
                    pan, tilt, _ = predict_product_pose(
                        fit, point.position, center, rotation_axis, tilt_axis,
                        product_rotation_deg, product_tilt_deg,
                    )

            payload = point.to_dict()
            payload["pan"] = round(float(pan) % 360.0, 2)
            payload["tilt"] = round(max(-60.0, min(60.0, float(tilt))), 2)
            for key in CAD_POINT_METADATA:
                if key in raw:
                    payload[key] = raw[key]
            calibrated_points.append(payload)

        self._set_points(record, calibrated_points)

        calibration = {
            "updated": now_iso(),
            "points_used": fit.points_used,
            "model": "planar_projective_residual" if fit.coordinate_axes is not None else "affine_3d",
            "rms_pan_error": round(fit.rms_pan_error, 4),
            "rms_tilt_error": round(fit.rms_tilt_error, 4),
            "origin": list(fit.origin),
            "scale": fit.scale,
            "product_rotation_deg": product_rotation_deg,
            "product_tilt_deg": product_tilt_deg,
            "product_center": [round(value, 6) for value in center],
            "product_spans": [round(value, 6) for value in spans],
            "rotation_axis": self._pose_axis_name(rotation_axis),
            "tilt_axis": self._pose_axis_name(tilt_axis),
            "reference_pose": {"rotation_deg": 0.0, "tilt_deg": 0.0},
        }
        context = self._context(record)
        context["product_rotation_deg"] = product_rotation_deg
        context["product_tilt_deg"] = product_tilt_deg
        context["calibration"] = calibration
        if context is record and record.get("source_kind") in CAD_PACKAGE_KINDS:
            record.setdefault("calibration_by_file", {})[
                str(record.get("selected_point_file") or "")
            ] = calibration
        record["updated"] = now_iso()
        self.save()
        return self._public_record(record, include_points=True)

    @_drawing_edit
    def reset_calibration(self, drawing_id: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        context = self._context(record)
        point_groups = (
            record.get("points_by_file", {}).values()
            if context is record and record.get("source_kind") in CAD_PACKAGE_KINDS
            else (self._points(record),)
        )
        for points in point_groups:
            for point in points:
                point["pan"] = None
                point["tilt"] = None
                point["calibration"] = False
                point["calibration_slot"] = None
                point.pop("calibration_pan", None)
                point.pop("calibration_tilt", None)
        context["calibration"] = None
        context["product_rotation_deg"] = 0.0
        context["product_tilt_deg"] = 0.0
        if context is not record:
            recipe = self.recipe_store.get_recipe(record["selected_recipe_id"])
            recipe.product_rotation_deg = recipe.product_tilt_deg = 0.0
            recipe.pose_applied = False
            cleared_ids = {point["id"] for point in context["points"]}
            recipe.points = [point for point in recipe.points if point.drawing_point_id not in cleared_ids]
            recipe.normalize_orders()
        elif record.get("source_kind") in CAD_PACKAGE_KINDS:
            record["calibration_by_file"] = {}
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

    def estimate_pan_tilt(
        self,
        drawing_id: str,
        x: float,
        y: float,
        z: float | None = None,
    ) -> dict[str, Any]:
        record = self._record(drawing_id)
        reference_points = self._reference_calibration_points(
            record,
            capture_zero_pose=False,
        )
        fit = fit_affine_calibration(reference_points)
        taught = [
            point
            for point in reference_points
            if point.calibration and point.has_pan_tilt
        ]
        target_z = (
            float(z)
            if z is not None
            else sum(point.z for point in taught) / len(taught)
        )
        context = self._context(record)
        product_rotation_deg = float(context.get("product_rotation_deg", 0.0) or 0.0)
        product_tilt_deg = float(context.get("product_tilt_deg", 0.0) or 0.0)
        posed, frame = self._posed_product_position(
            record,
            (float(x), float(y), target_z),
            product_rotation_deg,
            product_tilt_deg,
        )
        if product_rotation_deg or product_tilt_deg:
            pan, tilt, posed = predict_product_pose(
                fit, (float(x), float(y), target_z), frame["center"],
                frame["rotation_axis"], frame["tilt_axis"], product_rotation_deg, product_tilt_deg,
            )
        else:
            pan, tilt = fit.predict(DrawingPoint.create(posed))
        return {
            "pan": pan,
            "tilt": tilt,
            "x": float(x),
            "y": float(y),
            "z": target_z,
            "posed_x": posed[0],
            "posed_y": posed[1],
            "posed_z": posed[2],
            "product_rotation_deg": product_rotation_deg,
            "product_tilt_deg": product_tilt_deg,
            "rotation_axis": self._pose_axis_name(frame["rotation_axis"]),
            "tilt_axis": self._pose_axis_name(frame["tilt_axis"]),
            "points_used": fit.points_used,
            "model": "planar_projective_residual" if fit.coordinate_axes is not None else "affine_3d",
            "rms_pan_error": round(fit.rms_pan_error, 4),
            "rms_tilt_error": round(fit.rms_tilt_error, 4),
        }

    def estimate_recipe_coordinates(
        self,
        drawing_id: str,
        recipe_store: RecipeStore,
        recipe_id: str,
    ) -> dict[str, Any]:
        recipe = recipe_store.get_recipe(recipe_id)
        record = self._record(drawing_id)
        if recipe.drawing:
            # 다른 레시피의 활성 보정으로 계산하지 않는다.
            if recipe.drawing.get("product_id") != record.get("product_id"):
                raise ValueError("레시피와 도면이 일치하지 않습니다.")
            points = [DrawingPoint.from_dict(point) for point in recipe.drawing["points"]]
            fit = fit_inverse_xy_calibration(points)
            estimates = []
            for point in recipe.points:
                x, y = fit.predict(point.pan, point.tilt)
                estimates.append({"point_id": point.id, "x": x, "y": y, "z": None,
                                  "z_status": "unconfirmed", "z_reason": "Pan/Tilt does not determine contact Z.",
                                  "units": "drawing_units", "coordinate_frame": f"drawing:{drawing_id}",
                                  "points_used": fit.points_used,
                                  "rms_x_error": round(fit.rms_x_error, 4), "rms_y_error": round(fit.rms_y_error, 4)})
            return {"drawing_id": drawing_id, "recipe_id": recipe.id, "estimates": estimates}
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
        context = self._context(record)
        if recipe.drawing and (recipe.drawing.get("product_id") != record.get("product_id")
                               or recipe.drawing.get("point_file") != record.get("selected_point_file")):
            raise ValueError("다른 도면/그룹에 연결된 레시피에는 저장할 수 없습니다.")
        previous = deepcopy(recipe_store._recipes)
        updated = deepcopy(recipe)
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
                    for recipe_point in updated.points
                    if recipe_point.drawing_point_id == point["id"] or recipe_point.note.splitlines()[:1] == [source_key]
                ),
                None,
            )
            if existing is None:
                existing = RecipePoint.create(label, float(point["pan"]), float(point["tilt"]),
                                              order=len(updated.points) + 1, note=note)
                updated.points.append(existing)
                existing.enabled = bool(point.get("enabled", True))
            else:
                existing.name, existing.pan, existing.tilt, existing.note = label, float(point["pan"]), float(point["tilt"]), note
            existing.drawing_point_id = str(point["id"])
            existing.updated = now_iso()
            exported += 1
        if not exported:
            raise ValueError("저장할 Pan/Tilt 값이 없습니다. 기준점을 티칭하고 캘리브레이션하세요.")
        if record.get("source_kind") in CAD_PACKAGE_KINDS:
            state = self._new_recipe_state(record, record["selected_point_file"], self._points(record))
            for key in ("calibration", "product_rotation_deg", "product_tilt_deg"):
                state[key] = deepcopy(context.get(key, state[key]))
            updated.drawing = state
            updated.product_rotation_deg = state["product_rotation_deg"]
            updated.product_tilt_deg = state["product_tilt_deg"]
            updated.pose_applied = True
        updated.updated = now_iso()
        recipe_store._recipes = [updated if item.id == recipe.id else item for item in recipe_store._recipes]
        recipe_store._save_or_restore(previous)
        return {
            "exported": exported,
            "recipe": recipe_store.get_recipe(recipe_id).to_dict(),
        }

    def import_from_recipe(self, drawing_id: str, recipe_store: RecipeStore, recipe_id: str) -> dict[str, Any]:
        record = self._record(drawing_id)
        recipe = recipe_store.get_recipe(recipe_id)
        if recipe.drawing or (self.recipe_store is not None and record.get("source_kind") in CAD_PACKAGE_KINDS):
            self.recipe_store = recipe_store
            drawing = self.select_recipe(drawing_id, recipe.id)
            return {"imported": len(drawing["points"]), "drawing": drawing}
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
        context = self._context(record)
        if context is not record:
            return context["points"]
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
        context = self._context(record)
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
            "calibration": context.get("calibration"),
            "selected_recipe_id": record.get("selected_recipe_id", "") if context is not record else "",
            "product_rotation_deg": float(context.get("product_rotation_deg", 0.0) or 0.0),
            "product_tilt_deg": float(context.get("product_tilt_deg", 0.0) or 0.0),
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
            center, rotation_axis, tilt_axis, _ = self._product_pose_frame(record)
            result["pose_frame"] = {"center": list(center), "rotation_axis": rotation_axis, "tilt_axis": tilt_axis}
        if include_points:
            result["points"] = [
                self._public_point(point, index)
                for index, point in enumerate(self._points(record), start=1)
            ]
        if self.recipe_store is not None and record.get("source_kind") in CAD_PACKAGE_KINDS and context is record:
            result.update(calibration=None, product_rotation_deg=0.0, product_tilt_deg=0.0)
            for point in result.get("points", []):
                point.update(pan=None, tilt=None, calibration=False, calibration_slot=None)
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
                    "product_rotation_deg": float(old.get("product_rotation_deg", 0.0) or 0.0),
                    "product_tilt_deg": float(old.get("product_tilt_deg", 0.0) or 0.0),
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
        for calibration_key in ("calibration_pan", "calibration_tilt"):
            if calibration_key in old:
                point[calibration_key] = old[calibration_key]
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

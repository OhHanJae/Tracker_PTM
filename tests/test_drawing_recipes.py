import copy
import io
import json
import math
import threading
import zipfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pt503_tester.drawing_store import DrawingStore
from pt503_tester.recipe_store import RecipeStore


def fvp_archive():
    recipes = []
    for group in (1, 2, 3):
        recipes.append({"recipe_id": group, "name": f"Group {group}", "points": [
            {"point_id": index, "target_id": f"G{group}-{index}", "group_id": group,
             "xyz_mm": [x, y, 0.0], "enabled": True}
            for index, (x, y) in enumerate(((-100, -50), (100, -50), (-100, 50), (100, 50), (0, 0)), 1)
        ]})
    files = {
        "manifest.json": json.dumps({"format": "fvp-settings", "version": 1}).encode(),
        "settings/product_library/Panel/hash/manifest.json": json.dumps({
            "product_id": "Panel", "name": "Panel product", "coordinate_units": "mm",
            "model_units": "cm", "recipes": recipes}).encode(),
        "settings/product_library/Panel/hash/model.glb": b"glTF" + bytes(16),
        "settings/camera_calibration/camera.json": b'{"untouched":true}',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue(), files


def stores(tmp_path):
    recipes = RecipeStore(tmp_path / "recipes.json")
    drawings = DrawingStore(tmp_path / "drawings", recipe_store=recipes)
    drawing = drawings.create_from_uploads([("fvp.zip", fvp_archive()[0])])
    group_recipes = []
    for group in drawing["point_sets"]:
        drawings.select_point_set(drawing["id"], group["file"])
        recipe = recipes.upsert_recipe(name=group["name"])
        drawings.select_recipe(drawing["id"], recipe.id)
        group_recipes.append(recipe)
    drawings.select_point_set(drawing["id"], drawing["selected_point_file"])
    return drawings, recipes, drawing["id"], group_recipes


@pytest.mark.parametrize("existing_recipe", [False, True])
def test_fvp_upload_does_not_create_or_change_recipes(tmp_path, existing_recipe):
    recipes = RecipeStore(tmp_path / "recipes.json")
    if existing_recipe:
        recipes.upsert_recipe(name="Existing recipe")
    before = recipes.export_document()
    saved = recipes.path.read_bytes() if recipes.path.exists() else None
    drawings = DrawingStore(tmp_path / "drawings", recipe_store=recipes)
    for _ in range(2):
        drawing = drawings.create_from_uploads([("fvp.zip", fvp_archive()[0])])
        assert [group["point_count"] for group in drawing["point_sets"]] == [5, 5, 5]
        assert drawing["selected_recipe_id"] == ""
        # Export timestamps are generated on read; compare the unchanged document content.
        assert recipes.export_document() | {"updated": before["updated"]} == before
        assert (recipes.path.read_bytes() if recipes.path.exists() else None) == saved


def teach(drawings, drawing_id, offset=0):
    for point in drawings.get_drawing(drawing_id)["points"]:
        if point["x"] == point["y"] == 0:
            continue
        x, y = point["x"], point["y"]
        drawings.upsert_point(drawing_id, {
            "point_id": point["id"], "calibration": True,
            "pan": math.degrees(math.atan2(x, 1000)) % 360 + offset,
            "tilt": math.degrees(math.atan2(y, math.hypot(x, 1000))),
        })
    return drawings.calibrate(drawing_id)


def test_fvp_groups_and_recipe_calibrations_are_independent(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    assert [group["point_count"] for group in drawings.get_drawing(drawing_id)["point_sets"]] == [5, 5, 5]
    assert [recipe.name for recipe in linked] == ["Group 1", "Group 2", "Group 3"]
    first = linked[0].id
    drawings.select_recipe(drawing_id, first)
    calibrated = teach(drawings, drawing_id)
    second = recipes.upsert_recipe(name="Other pose").id
    empty = drawings.select_recipe(drawing_id, second)
    assert empty["calibration"] is None
    assert all(point["pan"] is None and not point["calibration"] for point in empty["points"])
    teach(drawings, drawing_id, offset=15)
    reloaded = drawings.select_recipe(drawing_id, first)
    assert reloaded["points"] == calibrated["points"]
    assert reloaded["calibration"] == calibrated["calibration"]
    drawings.select_point_set(drawing_id, "group_2.json")
    with pytest.raises(ValueError, match="다른 도면/그룹"):
        drawings.select_recipe(drawing_id, first)
    assert drawings.get_drawing(drawing_id)["calibration"] is None
    recipes = RecipeStore(recipes.path)
    drawings = DrawingStore(drawings.root, recipe_store=recipes)
    drawings.select_point_set(drawing_id, "group_1.json")
    assert drawings.select_recipe(drawing_id, first)["points"] == calibrated["points"]


def test_seed_package_keeps_selected_recipe_after_server_restart(tmp_path):
    uploaded, recipes, drawing_id, linked = stores(tmp_path)
    package_root = tmp_path / "packages"
    package = package_root / "Seed"
    (package / "model").mkdir(parents=True)
    (package / "coordinates").mkdir()
    assets = uploaded.root / drawing_id
    (package / "model" / "product.glb").write_bytes((assets / "product.glb").read_bytes())
    for name in ("coordinates_manifest.json", "group_1.json", "group_2.json", "group_3.json"):
        (package / "coordinates" / name).write_bytes((assets / name).read_bytes())
    drawings = DrawingStore(tmp_path / "seed_state", package_root, recipe_store=recipes)
    seed_id = drawings.list_drawings()[0]["id"]
    drawings.select_recipe(seed_id, linked[0].id)
    calibrated = teach(drawings, seed_id)
    restarted = DrawingStore(drawings.root, package_root, recipe_store=RecipeStore(recipes.path))
    restored = restarted.get_drawing(seed_id)
    assert restored["selected_recipe_id"] == linked[0].id
    assert restored["calibration"] == calibrated["calibration"]
    assert restored["points"] == calibrated["points"]


def test_pose_uses_depth_and_preserves_zero_reference(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    original = teach(drawings, drawing_id)
    transformed = drawings.calibrate(drawing_id, product_rotation_deg=30, product_tilt_deg=10)
    assert transformed["points"] != original["points"]
    for point in transformed["points"]:
        target = drawings.estimate_pan_tilt(drawing_id, point["x"], point["y"], point["z"])
        assert target["pan"] == point["pan"]
        assert target["tilt"] == point["tilt"]
        # Independent pinhole geometry: motor at (0, 0, -1000), pivot at CAD origin.
        a, b = math.radians(30), math.radians(10)
        x, y = point["x"], point["y"]
        # Tilt about local long X, then yaw about fixed vertical Z.
        rotated_x = x * math.cos(a) - y * math.cos(b) * math.sin(a)
        rotated_y = x * math.sin(a) + y * math.cos(b) * math.cos(a)
        depth = 1000 + y * math.sin(b)
        expected_pan = math.degrees(math.atan2(rotated_x, depth)) % 360
        expected_tilt = math.degrees(math.atan2(rotated_y, math.hypot(rotated_x, depth)))
        pan_error = (point["pan"] - expected_pan + 180) % 360 - 180
        assert abs(pan_error) < 0.04
        assert abs(point["tilt"] - expected_tilt) < 0.04
    restored = drawings.calibrate(drawing_id)
    assert restored["points"] == original["points"]
    drawings.export_to_recipe(drawing_id, recipes, linked[0].id)
    first_point = recipes.get_recipe(linked[0].id).points[0]
    recipes.upsert_point(linked[0].id, point_id=first_point.id, name=first_point.name,
                         pan=first_point.pan, tilt=first_point.tilt, pan_speed=17, dwell_ms=123)
    drawings.export_to_recipe(drawing_id, recipes, linked[0].id)
    updated = recipes.get_recipe(linked[0].id)
    assert len(updated.points) == 5
    assert updated.points[0].id == first_point.id
    assert updated.points[0].pan_speed == 17
    assert updated.points[0].dwell_ms == 123


def test_zip_roundtrip_preserves_fvp_assets_and_all_recipe_corrections(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    teach(drawings, drawing_id)
    drawings.export_to_recipe(drawing_id, recipes, linked[0].id)
    drawings.select_point_set(drawing_id, "group_2.json")
    drawings.select_recipe(drawing_id, linked[1].id)
    teach(drawings, drawing_id, offset=10)
    before = drawings.calibrate(drawing_id, product_rotation_deg=15)
    output = drawings.archive(drawing_id)
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        for name, payload in fvp_archive()[1].items():
            assert archive.read(name) == payload
    new_recipes = RecipeStore(tmp_path / "restored_recipes.json")
    restored = DrawingStore(tmp_path / "restored_drawings", recipe_store=new_recipes)
    drawing = restored.create_from_uploads([("restored.zip", output)])
    assert drawing["selected_recipe_id"] == linked[1].id
    assert drawing["points"] == before["points"]
    assert drawing["product_rotation_deg"] == 15
    assert len([recipe for recipe in new_recipes._recipes if recipe.drawing]) == 3
    restored.select_point_set(drawing["id"], "group_1.json")
    assert restored.select_recipe(drawing["id"], linked[0].id)["calibration"]["points_used"] == 4
    # Point mapping survives the new drawing UUID without duplicate recipe points.
    restored.export_to_recipe(drawing["id"], new_recipes, linked[0].id)
    assert len(new_recipes.get_recipe(linked[0].id).points) == 5


def test_recipe_json_carries_calibration_and_validates_before_import(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    before = teach(drawings, drawing_id)
    payload = recipes.export_document(linked[0].id)
    replacement = RecipeStore(tmp_path / "imported.json")
    replacement.import_document(payload)
    drawings.recipe_store = replacement
    assert drawings.select_recipe(drawing_id, linked[0].id)["points"] == before["points"]
    invalid = copy.deepcopy(payload)
    invalid["recipes"][0]["drawing"]["points"][0]["x"] = float("nan")
    with pytest.raises(ValueError):
        replacement.import_document(invalid)
    assert replacement.export_document(linked[0].id)["recipes"] == payload["recipes"]


def test_invalid_point_edits_and_export_failures_leave_recipe_unchanged(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    before = teach(drawings, drawing_id)
    for params in ({"pan": float("nan")}, {"tilt": 80}, {"x": 9}):
        with pytest.raises(ValueError):
            drawings.upsert_point(drawing_id, {"point_id": before["points"][0]["id"], **params})
        assert drawings.get_drawing(drawing_id)["points"] == before["points"]
    previous = recipes.export_document()["recipes"]
    with patch.object(recipes, "save", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            drawings.export_to_recipe(drawing_id, recipes, linked[0].id)
    assert recipes.export_document()["recipes"] == previous


def test_reset_only_clears_selected_recipe(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    first = linked[0].id
    drawings.select_recipe(drawing_id, first)
    calibrated = teach(drawings, drawing_id)
    other = recipes.upsert_recipe(name="Other").id
    drawings.select_recipe(drawing_id, other)
    teach(drawings, drawing_id, offset=10)
    cleared = drawings.reset_calibration(drawing_id)
    assert all(point["pan"] is None and not point["calibration"] for point in cleared["points"])
    assert drawings.select_recipe(drawing_id, first)["points"] == calibrated["points"]


def test_restore_into_same_store_keeps_existing_recipes(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    teach(drawings, drawing_id)
    drawings.export_to_recipe(drawing_id, recipes, linked[0].id)
    previous = copy.deepcopy(recipes.get_recipe(linked[0].id).to_dict())
    restored = drawings.create_from_uploads([("copy.zip", drawings.archive(drawing_id))])
    assert restored["selected_recipe_id"] != linked[0].id
    assert recipes.get_recipe(linked[0].id).to_dict() == previous
    assert recipes.get_recipe(restored["selected_recipe_id"]).drawing["drawing_id"] == restored["id"]


def test_teaching_save_failure_rolls_back_and_recipe_point_edits_sync(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    teach(drawings, drawing_id)
    drawings.export_to_recipe(drawing_id, recipes, linked[0].id)
    previous = recipes.export_document()["recipes"]
    first = recipes.get_recipe(linked[0].id).points[0]
    with patch.object(recipes, "save", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            drawings.upsert_point(drawing_id, {"point_id": first.drawing_point_id, "pan": 20, "tilt": 1})
    assert recipes.export_document()["recipes"] == previous
    recipes.upsert_point(linked[0].id, point_id=first.id, name=first.name, pan=20, tilt=1)
    point = drawings.select_recipe(drawing_id, linked[0].id)["points"][0]
    assert point["pan"] == 20
    assert point["tilt"] == 1
    assert point.get("calibration_pan") is None
    assert drawings.get_drawing(drawing_id)["calibration"] is None


def test_failed_calculation_keeps_reference_and_other_recipes(tmp_path):
    drawings, recipes, drawing_id, linked = stores(tmp_path)
    drawings.select_recipe(drawing_id, linked[0].id)
    point = drawings.get_drawing(drawing_id)["points"][0]
    drawings.upsert_point(drawing_id, {"point_id": point["id"], "pan": 1, "tilt": 2, "calibration": True})
    previous = recipes.export_document()["recipes"]
    with pytest.raises(ValueError):
        drawings.calibrate(drawing_id)
    assert recipes.export_document()["recipes"] == previous


def test_http_zip_upload_recipe_selection_and_download(tmp_path):
    from pt503_tester.headless_server import HeadlessController, HeadlessHttpServer, WebHandler

    recipes = RecipeStore(tmp_path / "http_recipes.json")
    drawings = DrawingStore(tmp_path / "http_drawings", recipe_store=recipes)
    controller = HeadlessController(recipes, drawing_store=drawings, settings_path=tmp_path / "settings.json")
    controller.auto_reconnect_config["enabled"] = False
    with patch.object(WebHandler, "controller", controller, create=True):
        server = HeadlessHttpServer(("127.0.0.1", 0), WebHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def command(name, params):
            request = Request(base + "/api/command", json.dumps({"command": name, "params": params}).encode(),
                              {"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)["result"]

        try:
            boundary = "ptm-drawing-test"
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fvp.zip"\r\n'
                    'Content-Type: application/zip\r\n\r\n').encode() + fvp_archive()[0] + f"\r\n--{boundary}--\r\n".encode()
            request = Request(base + "/api/drawings/upload", body,
                              {"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urlopen(request, timeout=5) as response:
                drawing = json.load(response)["result"]["drawing"]
            assert len(drawing["point_sets"]) == 3
            assert not [recipe for recipe in recipes._recipes if recipe.drawing]
            for group in drawing["point_sets"]:
                recipe = command("recipe.upsert", {"name": group["name"]})["recipe"]
                command("drawing.select_point_set", {"drawing_id": drawing["id"], "point_file": group["file"]})
                command("drawing.select_recipe", {"drawing_id": drawing["id"], "recipe_id": recipe["id"]})
            linked = [recipe for recipe in recipes._recipes if recipe.drawing]
            command("drawing.select_point_set", {"drawing_id": drawing["id"], "point_file": drawing["selected_point_file"]})
            chosen = command("drawing.select_recipe", {"drawing_id": drawing["id"], "recipe_id": linked[0].id})["drawing"]
            assert chosen["selected_recipe_id"] == linked[0].id
            with pytest.raises(HTTPError) as error:
                command("drawing.point_upsert", {"drawing_id": drawing["id"], "recipe_id": linked[1].id,
                                                  "point_id": chosen["points"][0]["id"], "pan": 1, "tilt": 2})
            assert error.value.code == 400
            with urlopen(base + f'/api/drawings/{drawing["id"]}/archive', timeout=5) as response:
                assert response.headers["Content-Type"] == "application/zip"
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    state = json.loads(archive.read("monitor_drawing_state.json"))
                    assert len(state["recipe_document"]["recipes"]) == 3
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
            controller.shutdown()

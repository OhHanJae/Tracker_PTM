"""Persistent recipe/point coordinate table shared by GUI and headless server."""

from __future__ import annotations

import json
import math
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


RECIPE_SCHEMA = "pt503.recipe.bundle"
RECIPE_VERSION = 1


def app_state_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "OSRND" / "PT503Tester"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class RecipePoint:
    id: str
    name: str
    pan: float
    tilt: float
    pan_speed: int = 32
    tilt_speed: int = 32
    dwell_ms: int = 0
    order: int = 1
    enabled: bool = True
    note: str = ""
    updated: str = field(default_factory=now_iso)

    @classmethod
    def create(
        cls,
        name: str,
        pan: float,
        tilt: float,
        *,
        order: int,
        pan_speed: int = 32,
        tilt_speed: int = 32,
        dwell_ms: int = 0,
        note: str = "",
    ) -> "RecipePoint":
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            pan=float(pan) % 360.0,
            tilt=max(-60.0, min(60.0, float(tilt))),
            pan_speed=int(pan_speed),
            tilt_speed=int(tilt_speed),
            dwell_ms=int(dwell_ms),
            order=int(order),
            note=note,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_order: int) -> "RecipePoint":
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            name=str(data.get("name") or f"P{default_order}"),
            pan=float(data.get("pan", 0.0)) % 360.0,
            tilt=max(-60.0, min(60.0, float(data.get("tilt", 0.0)))),
            pan_speed=int(data.get("pan_speed", 32)),
            tilt_speed=int(data.get("tilt_speed", 32)),
            dwell_ms=max(0, int(data.get("dwell_ms", 0))),
            order=int(data.get("order", default_order)),
            enabled=bool(data.get("enabled", True)),
            note=str(data.get("note", "")),
            updated=str(data.get("updated") or now_iso()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "pan": round(self.pan % 360.0, 2),
            "tilt": round(max(-60.0, min(60.0, self.tilt)), 2),
            "pan_speed": int(self.pan_speed),
            "tilt_speed": int(self.tilt_speed),
            "dwell_ms": int(self.dwell_ms),
            "order": int(self.order),
            "enabled": bool(self.enabled),
            "note": self.note,
            "updated": self.updated,
        }


@dataclass
class Recipe:
    id: str
    name: str
    description: str = ""
    points: list[RecipePoint] = field(default_factory=list)
    updated: str = field(default_factory=now_iso)

    @classmethod
    def create(cls, name: str, description: str = "") -> "Recipe":
        return cls(id=str(uuid.uuid4()), name=name, description=description)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Recipe":
        points_data = data.get("points", [])
        points = [
            RecipePoint.from_dict(item, index + 1)
            for index, item in enumerate(points_data)
            if isinstance(item, dict)
        ]
        recipe = cls(
            id=str(data.get("id") or uuid.uuid4()),
            name=str(data.get("name") or "Recipe"),
            description=str(data.get("description", "")),
            points=points,
            updated=str(data.get("updated") or now_iso()),
        )
        recipe.normalize_orders()
        return recipe

    def normalize_orders(self) -> None:
        self.points.sort(key=lambda item: (item.order, item.name))
        for index, point in enumerate(self.points, start=1):
            point.order = index

    def to_dict(self) -> dict[str, Any]:
        self.normalize_orders()
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "updated": self.updated,
            "points": [point.to_dict() for point in self.points],
        }


class RecipeStore:
    """JSON-backed recipe/point table.

    This intentionally does not write Pelco presets.  Points are command
    targets for direct pan/tilt absolute movement.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else app_state_dir() / "recipes.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._recipes: list[Recipe] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._recipes = [Recipe.create("Default")]
            self.save()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._recipes = [Recipe.create("Default")]
            return
        recipes_data = data.get("recipes", []) if isinstance(data, dict) else []
        self._recipes = [
            Recipe.from_dict(item) for item in recipes_data if isinstance(item, dict)
        ]
        if not self._recipes:
            self._recipes = [Recipe.create("Default")]

    def save(self) -> None:
        payload = {
            "schema": RECIPE_SCHEMA,
            "version": RECIPE_VERSION,
            "updated": now_iso(),
            "recipes": [recipe.to_dict() for recipe in self._recipes],
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def export_document(self, recipe_id: str | None = None) -> dict[str, Any]:
        recipes = [self.get_recipe(recipe_id)] if recipe_id else list(self._recipes)
        return {
            "schema": RECIPE_SCHEMA,
            "version": RECIPE_VERSION,
            "updated": now_iso(),
            "recipes": [recipe.to_dict() for recipe in recipes],
        }

    def import_document(self, payload: Any) -> list[dict[str, Any]]:
        imported = self._validated_document(payload)
        imported_ids = {recipe.id for recipe in imported}
        merged = [recipe for recipe in self._recipes if recipe.id not in imported_ids]
        merged.extend(imported)
        self._validate_unique_ids(merged)

        previous = self._recipes
        self._recipes = merged
        try:
            self.save()
        except OSError:
            self._recipes = previous
            raise
        return [recipe.to_dict() for recipe in imported]

    def list_recipes(self) -> list[dict[str, Any]]:
        return [recipe.to_dict() for recipe in self._recipes]

    def get_recipe(self, recipe_id: str | None = None) -> Recipe:
        if recipe_id:
            for recipe in self._recipes:
                if recipe.id == recipe_id:
                    return recipe
            raise KeyError(f"unknown recipe: {recipe_id}")
        return self._recipes[0]

    def upsert_recipe(
        self,
        *,
        recipe_id: str | None = None,
        name: str,
        description: str = "",
    ) -> Recipe:
        if recipe_id:
            recipe = self.get_recipe(recipe_id)
            recipe.name = name
            recipe.description = description
            recipe.updated = now_iso()
        else:
            recipe = Recipe.create(name, description)
            self._recipes.append(recipe)
        self.save()
        return recipe

    def delete_recipe(self, recipe_id: str) -> None:
        if len(self._recipes) <= 1:
            raise ValueError("at least one recipe is required")
        self._recipes = [recipe for recipe in self._recipes if recipe.id != recipe_id]
        self.save()

    def upsert_point(
        self,
        recipe_id: str,
        *,
        point_id: str | None = None,
        name: str,
        pan: float,
        tilt: float,
        pan_speed: int = 32,
        tilt_speed: int = 32,
        dwell_ms: int = 0,
        note: str = "",
        enabled: bool = True,
    ) -> RecipePoint:
        recipe = self.get_recipe(recipe_id)
        if point_id:
            point = self.get_point(recipe_id, point_id)
            point.name = name
            point.pan = float(pan) % 360.0
            point.tilt = max(-60.0, min(60.0, float(tilt)))
            point.pan_speed = int(pan_speed)
            point.tilt_speed = int(tilt_speed)
            point.dwell_ms = max(0, int(dwell_ms))
            point.note = note
            point.enabled = enabled
            point.updated = now_iso()
        else:
            point = RecipePoint.create(
                name,
                pan,
                tilt,
                order=len(recipe.points) + 1,
                pan_speed=pan_speed,
                tilt_speed=tilt_speed,
                dwell_ms=dwell_ms,
                note=note,
            )
            point.enabled = enabled
            recipe.points.append(point)
        recipe.updated = now_iso()
        recipe.normalize_orders()
        self.save()
        return point

    def get_point(self, recipe_id: str, point_id: str) -> RecipePoint:
        recipe = self.get_recipe(recipe_id)
        for point in recipe.points:
            if point.id == point_id:
                return point
        raise KeyError(f"unknown point: {point_id}")

    def delete_point(self, recipe_id: str, point_id: str) -> None:
        recipe = self.get_recipe(recipe_id)
        recipe.points = [point for point in recipe.points if point.id != point_id]
        recipe.updated = now_iso()
        recipe.normalize_orders()
        self.save()

    def reorder_points(self, recipe_id: str, ordered_ids: list[str]) -> Recipe:
        recipe = self.get_recipe(recipe_id)
        by_id = {point.id: point for point in recipe.points}
        if len(ordered_ids) != len(by_id) or set(by_id) != set(ordered_ids):
            raise ValueError("ordered_ids must contain every point id exactly once")
        recipe.points = [by_id[item] for item in ordered_ids]
        for order, point in enumerate(recipe.points, start=1):
            point.order = order
        recipe.updated = now_iso()
        recipe.normalize_orders()
        self.save()
        return recipe

    @classmethod
    def _validated_document(cls, payload: Any) -> list[Recipe]:
        if not isinstance(payload, dict):
            raise ValueError("recipe document must be an object")
        schema = payload.get("schema")
        if schema not in (None, RECIPE_SCHEMA):
            raise ValueError(f"unsupported recipe schema: {schema}")
        if payload.get("version") != RECIPE_VERSION:
            raise ValueError(f"unsupported recipe version: {payload.get('version')}")
        recipes_data = payload.get("recipes")
        if not isinstance(recipes_data, list) or not recipes_data:
            raise ValueError("recipes must be a non-empty array")

        recipes: list[Recipe] = []
        for recipe_index, data in enumerate(recipes_data, start=1):
            if not isinstance(data, dict):
                raise ValueError(f"recipe {recipe_index} must be an object")
            cls._required_string(data, "id", f"recipe {recipe_index}")
            cls._required_string(data, "name", f"recipe {recipe_index}")
            if "description" in data and not isinstance(data["description"], str):
                raise ValueError(f"recipe {recipe_index}.description must be a string")
            points_data = data.get("points")
            if not isinstance(points_data, list):
                raise ValueError(f"recipe {recipe_index}.points must be an array")
            orders: set[int] = set()
            for point_index, point in enumerate(points_data, start=1):
                where = f"recipe {recipe_index}.point {point_index}"
                if not isinstance(point, dict):
                    raise ValueError(f"{where} must be an object")
                cls._required_string(point, "id", where)
                cls._required_string(point, "name", where)
                cls._finite_number(point, "pan", where, minimum=0.0, maximum=359.99)
                cls._finite_number(point, "tilt", where, minimum=-60.0, maximum=60.0)
                cls._integer(point, "pan_speed", where, minimum=0, maximum=63)
                cls._integer(point, "tilt_speed", where, minimum=0, maximum=63)
                cls._integer(point, "dwell_ms", where, minimum=0, maximum=3_600_000)
                order = cls._integer(point, "order", where, minimum=1)
                if order in orders:
                    raise ValueError(f"{where}.order is duplicated: {order}")
                orders.add(order)
                if not isinstance(point.get("enabled"), bool):
                    raise ValueError(f"{where}.enabled must be boolean")
                if not isinstance(point.get("note"), str):
                    raise ValueError(f"{where}.note must be a string")
            recipes.append(Recipe.from_dict(data))
        cls._validate_unique_ids(recipes)
        return recipes

    @staticmethod
    def _validate_unique_ids(recipes: list[Recipe]) -> None:
        recipe_ids: set[str] = set()
        point_ids: set[str] = set()
        for recipe in recipes:
            if recipe.id in recipe_ids:
                raise ValueError(f"duplicate recipe id: {recipe.id}")
            recipe_ids.add(recipe.id)
            for point in recipe.points:
                if point.id in point_ids:
                    raise ValueError(f"duplicate point id: {point.id}")
                point_ids.add(point.id)

    @staticmethod
    def _required_string(data: dict[str, Any], key: str, where: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}.{key} must be a non-empty string")
        return value

    @staticmethod
    def _finite_number(
        data: dict[str, Any],
        key: str,
        where: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}.{key} must be numeric")
        number = float(value)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(f"{where}.{key} must be between {minimum} and {maximum}")
        return number

    @staticmethod
    def _integer(
        data: dict[str, Any],
        key: str,
        where: str,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}.{key} must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            suffix = f"..{maximum}" if maximum is not None else f" or greater"
            raise ValueError(f"{where}.{key} must be {minimum}{suffix}")
        return value

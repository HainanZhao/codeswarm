from __future__ import annotations

from collections.abc import Iterator
import copy
from functools import cached_property
from json import dumps
from typing import Callable, Required, TypedDict

from wingmen._loop import loop_last


class SchemaDict(TypedDict, total=False):
    """A schema entry used to supply runtime settings defaults."""

    key: Required[str]
    title: str
    type: Required[str]
    help: str
    choices: list[str] | list[tuple[str, str]] | None
    default: object
    fields: list[SchemaDict]
    validate: list[dict]
    editable: bool
    advanced: bool


class SettingsError(Exception):
    """Base class for settings errors."""


class InvalidValue(SettingsError):
    """A setting was not of the expected type."""


def parse_key(key: str) -> list[str]:
    return key.split(".")


class Schema:
    def __init__(self, schema: list[SchemaDict]) -> None:
        self.schema = schema

    def get_default(self, key: str) -> object | None:
        """Get a default value for a dotted setting key."""
        schema_object: object = self.defaults
        for last, sub_key in loop_last(parse_key(key)):
            if last:
                if isinstance(schema_object, dict):
                    return schema_object.get(sub_key)
                return None
            if isinstance(schema_object, dict):
                schema_object = schema_object.get(sub_key, {})
            else:
                return None
        return None

    @cached_property
    def defaults(self) -> dict[str, object]:
        settings: dict[str, object] = {}

        def set_defaults(
            schema: list[SchemaDict], target: dict[str, object]
        ) -> None:
            for sub_schema in schema:
                key = sub_schema["key"]
                if sub_schema["type"] == "object":
                    if fields := sub_schema.get("fields"):
                        child: dict[str, object] = {}
                        target[key] = child
                        set_defaults(fields, child)
                elif (default := sub_schema.get("default")) is not None:
                    target[key] = default

        set_defaults(self.schema, settings)
        return settings

    @property
    def keys(self) -> list[str]:
        """Return all leaf setting keys for startup callbacks."""
        keys: list[str] = []

        def collect(schema: list[SchemaDict], prefix: str = "") -> None:
            for sub_schema in schema:
                key = f"{prefix}.{sub_schema['key']}" if prefix else sub_schema["key"]
                if sub_schema["type"] == "object":
                    collect(sub_schema.get("fields", []), key)
                else:
                    keys.append(key)

        collect(self.schema)
        return keys


class Settings:
    """Runtime settings backed by a JSON-compatible dictionary."""

    def __init__(
        self,
        schema: Schema,
        settings: dict[str, object],
        on_set_callback: Callable[[str, object], None] | None = None,
    ) -> None:
        self._schema = schema
        self._settings = settings
        self._on_set_callback = on_set_callback
        self._changed = False

    @property
    def changed(self) -> bool:
        return self._changed

    def up_to_date(self) -> None:
        """Clear the dirty flag after persistence."""
        self._changed = False

    @property
    def json(self) -> str:
        """Serialize settings."""
        return dumps(self._settings, indent=4, separators=(", ", ": "))

    def set_all(self) -> None:
        if self._on_set_callback is not None:
            for key in self._schema.keys:
                self._on_set_callback(key, self.get(key))

    def get[ExpectType](
        self,
        key: str,
        expect_type: type[ExpectType] = object,  # type: ignore[assignment]
        *,
        expand: bool = True,
    ) -> ExpectType:
        from os.path import expandvars

        settings: object = self._settings
        for last, sub_key in loop_last(parse_key(key)):
            if last:
                if not isinstance(settings, dict):
                    settings = {}
                value = settings.get(sub_key)
                if value is None:
                    value = self._schema.get_default(key)
                    if value is None:
                        value = expect_type()
                elif isinstance(value, str) and expand:
                    value = expandvars(value)
                if not isinstance(value, expect_type):
                    value = expect_type(value)  # type: ignore[call-arg]
                if not isinstance(value, expect_type):
                    raise InvalidValue(
                        f"key {sub_key!r} is not of expected type {expect_type.__name__}"
                    )
                return value
            if not isinstance(settings, dict):
                settings = {}
            settings = settings.get(sub_key, {})

        raise AssertionError("setting key must contain a leaf")

    def set(self, key: str, value: object) -> None:
        """Set a setting and notify the app if it changed."""
        current_value: object = self.get(key, expand=False)
        updated_settings = copy.deepcopy(self._settings)
        setting: object = updated_settings

        for last, sub_key in loop_last(parse_key(key)):
            if not isinstance(setting, dict):
                setting = {}
            if last:
                if current_value != value:
                    self._changed = True
                    self._settings = updated_settings
                setting[sub_key] = value
            else:
                child = setting.setdefault(sub_key, {})
                if not isinstance(child, dict):
                    child = {}
                    setting[sub_key] = child
                setting = child

        if self._on_set_callback is not None:
            self._on_set_callback(key, value)

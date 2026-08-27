"""不変マッピングのフィールド型。

pydantic の `frozen=True` が止めるのはフィールドの再代入だけで、公開した
dict の中身は書き換えられる。state / config を strategy へ read-only で
渡す契約を型で守るために、構築時に MappingProxyType へ包む。
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeVar

_K = TypeVar("_K")
_V = TypeVar("_V")


def freeze_mapping(value: Mapping[_K, _V]) -> Mapping[_K, _V]:
    return MappingProxyType(dict(value))

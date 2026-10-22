"""Shared result type for fallible operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True)
class Ok(Generic[T, E]):
    value: T


@dataclass(frozen=True)
class Err(Generic[T, E]):
    error: E


Result = Ok[T, E] | Err[T, E]

"""Shared fixtures for the udi_iox test suite.

The lightweight stand-ins for pyisyox's :class:`Controller` and
:class:`Node` live in :mod:`tests._fakes` so test modules can import
the dataclasses directly. These fixtures wire them up for tests that
prefer the pytest-fixture style.
"""

from __future__ import annotations

import pytest

from tests._fakes import (
    FakeController,
    FakeEvent,
    FakeLifecycleEvent,
    FakeNode,
    FakeNodePropertyValue,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the test runner to load the udi_iox custom integration."""


@pytest.fixture
def fake_node_factory():
    """Build a FakeNode with sensible defaults; override per test."""
    return FakeNode


@pytest.fixture
def fake_property_factory():
    """Build a FakeNodePropertyValue."""
    return FakeNodePropertyValue


@pytest.fixture
def fake_controller():
    """Return a fresh FakeController per test."""
    return FakeController()


@pytest.fixture
def fake_event_factory():
    return FakeEvent


@pytest.fixture
def fake_lifecycle_factory():
    return FakeLifecycleEvent

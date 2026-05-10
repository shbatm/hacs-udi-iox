"""Snapshot tests for the udi_iox cover platform — currently blocked.

The cover platform exercises a pre-existing pyisyox-6 migration gap
that crashes ``async_setup_entry`` end-to-end:

* No native node attribute classifies a cover; the only path is the
  pyisyox plugin classifier (``ControllablePlatform.COVER``), which
  needs a real node-server profile loaded onto the controller. The
  in-process FakeController doesn't carry a ``profile`` yet.

Once that's fixed upstream, drop the ``skip`` and let the snapshot harness
populate ``tests/snapshots/test_cover.ambr``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "cover requires plugin classifier + profile; FakeController doesn't "
        "model that yet"
    )
)

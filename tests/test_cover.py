"""Snapshot tests for the udi_iox cover platform — currently blocked.

The cover platform exercises a pre-existing pyisyox-6 migration gap
that crashes ``async_setup_entry`` end-to-end:

* No native node attribute classifies a cover; the only path is the
  pyisyox plugin classifier (``ControllablePlatform.COVER``), which
  needs a PG3 plugin's nodedef present in the loaded profile. The
  bundled ``eisy6_profile.json`` only carries the stock Insteon /
  Z-Wave nodedefs.

Once a PG3 cover plugin fixture lands, drop the ``skip`` and let the
snapshot harness populate ``tests/snapshots/test_cover.ambr``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "cover requires plugin classifier + PG3 nodedef in the loaded profile; "
        "bundled eisy6 profile doesn't carry one yet"
    )
)

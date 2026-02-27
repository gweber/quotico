"""
backend/tests/test_admin_legacy_team_routes_removed.py

Purpose:
    Ensure legacy Team Tower admin routes are removed and only teams-v3 routes
    remain active.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "backend")

from app.routers.admin import router as admin_router


def test_legacy_team_routes_removed() -> None:
    paths = {route.path for route in admin_router.routes}

    assert "/api/admin/teams" not in paths
    assert "/api/admin/teams/{team_id}" not in paths
    assert "/api/admin/teams/{team_id}/aliases" not in paths
    assert "/api/admin/teams/{team_id}/merge" not in paths
    assert "/api/admin/teams/alias-suggestions" not in paths
    assert "/api/admin/teams/alias-suggestions/apply" not in paths
    assert "/api/admin/teams/alias-suggestions/{suggestion_id}/reject" not in paths

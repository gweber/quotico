"""
backend/app/services/admin_view_catalog_service.py

Purpose:
    Central server-side registry for frontend view catalog metadata used by
    admin tooling and runtime observability.

Dependencies:
    - app.utils.utcnow
"""

from __future__ import annotations

from typing import Any

from app.utils import utcnow


_VIEW_CATALOG: list[dict[str, Any]] = [
    {"id": "dashboard", "name_key": "admin.viewsCatalog.names.dashboard", "route_name": "dashboard", "path": "/", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "login", "name_key": "admin.viewsCatalog.names.login", "route_name": "login", "path": "/login", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "register", "name_key": "admin.viewsCatalog.names.register", "route_name": "register", "path": "/register", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "complete_profile", "name_key": "admin.viewsCatalog.names.completeProfile", "route_name": "complete-profile", "path": "/complete-profile", "group": "public", "requires_auth": True, "requires_admin": False, "enabled": True},
    {"id": "leaderboard", "name_key": "admin.viewsCatalog.names.leaderboard", "route_name": "leaderboard", "path": "/leaderboard", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "join_squad", "name_key": "admin.viewsCatalog.names.joinSquad", "route_name": "join-squad", "path": "/join/:code", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "squads", "name_key": "admin.viewsCatalog.names.squads", "route_name": "squads", "path": "/squads", "group": "public", "requires_auth": True, "requires_admin": False, "enabled": True},
    {"id": "squad_detail", "name_key": "admin.viewsCatalog.names.squadDetail", "route_name": "squad-detail", "path": "/squads/:id", "group": "public", "requires_auth": True, "requires_admin": False, "enabled": True},
    {"id": "squad_war_room", "name_key": "admin.viewsCatalog.names.squadWarRoom", "route_name": "squad-war-room", "path": "/squads/:id/war-room/:matchId", "group": "public", "requires_auth": True, "requires_admin": False, "enabled": True},
    {"id": "battles", "name_key": "admin.viewsCatalog.names.battles", "route_name": "battles", "path": "/battles", "group": "public", "requires_auth": True, "requires_admin": False, "enabled": True},
    {"id": "matchday", "name_key": "admin.viewsCatalog.names.matchday", "route_name": "matchday", "path": "/matchday/:sport?/:matchday?", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "qbot", "name_key": "admin.viewsCatalog.names.qbot", "route_name": "qbot", "path": "/qbot", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "qtip_performance", "name_key": "admin.viewsCatalog.names.qtipPerformance", "route_name": "qtip-performance", "path": "/qtip-performance", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "settings", "name_key": "admin.viewsCatalog.names.settings", "route_name": "settings", "path": "/settings", "group": "public", "requires_auth": True, "requires_admin": False, "enabled": True},
    {"id": "analysis", "name_key": "admin.viewsCatalog.names.analysis", "route_name": "analysis", "path": "/analysis/:league?", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "legal", "name_key": "admin.viewsCatalog.names.legal", "route_name": "legal", "path": "/legal/:section", "group": "public", "requires_auth": False, "requires_admin": False, "enabled": True},
    {"id": "admin_dashboard", "name_key": "admin.viewsCatalog.names.adminDashboard", "route_name": "admin-dashboard", "path": "/admin", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_ingest", "name_key": "admin.viewsCatalog.names.adminIngest", "route_name": "admin-ingest", "path": "/admin/ingest", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_users", "name_key": "admin.viewsCatalog.names.adminUsers", "route_name": "admin-users", "path": "/admin/users", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_matches", "name_key": "admin.viewsCatalog.names.adminMatches", "route_name": "admin-matches", "path": "/admin/matches", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_match_detail", "name_key": "admin.viewsCatalog.names.adminMatchDetail", "route_name": "admin-match-detail", "path": "/admin/matches/:matchId", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_battles", "name_key": "admin.viewsCatalog.names.adminBattles", "route_name": "admin-battles", "path": "/admin/battles", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_audit", "name_key": "admin.viewsCatalog.names.adminAudit", "route_name": "admin-audit", "path": "/admin/audit", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_event_bus", "name_key": "admin.viewsCatalog.names.adminEventBus", "route_name": "admin-event-bus", "path": "/admin/event-bus", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_time_machine_justice", "name_key": "admin.viewsCatalog.names.adminTimeMachineJustice", "route_name": "admin-time-machine-justice", "path": "/admin/time-machine-justice", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_team_tower", "name_key": "admin.viewsCatalog.names.adminTeamTower", "route_name": "admin-team-tower", "path": "/admin/team-tower", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_qbot_lab", "name_key": "admin.viewsCatalog.names.adminQbotLab", "route_name": "admin-qbot-lab", "path": "/admin/qbot-lab", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_qbot_strategy_detail", "name_key": "admin.viewsCatalog.names.adminQbotStrategyDetail", "route_name": "admin-qbot-strategy-detail", "path": "/admin/qbot/strategies/:strategyId", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_provider_status", "name_key": "admin.viewsCatalog.names.adminProviderStatus", "route_name": "admin-provider-status", "path": "/admin/provider-status", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_leagues", "name_key": "admin.viewsCatalog.names.adminLeagues", "route_name": "admin-leagues", "path": "/admin/leagues", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_qtip_trace", "name_key": "admin.viewsCatalog.names.adminQtipTrace", "route_name": "admin-qtip-trace", "path": "/admin/qtip-trace/:matchId", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_data_audit", "name_key": "admin.viewsCatalog.names.adminDataAudit", "route_name": "admin-data-audit", "path": "/admin/data-audit", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
    {"id": "admin_views_catalog", "name_key": "admin.viewsCatalog.names.adminViewsCatalog", "route_name": "admin-views-catalog", "path": "/admin/views", "group": "admin", "requires_auth": True, "requires_admin": True, "enabled": True},
]


def list_view_catalog() -> dict[str, Any]:
    items = sorted(_VIEW_CATALOG, key=lambda row: (str(row["group"]), str(row["path"])))
    summary = {
        "total": len(items),
        "public": sum(1 for row in items if row["group"] == "public"),
        "admin": sum(1 for row in items if row["group"] == "admin"),
        "auth_required": sum(1 for row in items if bool(row["requires_auth"])),
        "admin_required": sum(1 for row in items if bool(row["requires_admin"])),
    }
    return {
        "generated_at_utc": utcnow().isoformat(),
        "summary": summary,
        "items": items,
    }


"""
backend/tests/test_admin_views_catalog.py

Purpose:
    Router and service tests for the admin views catalog endpoint.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers.admin import router as admin_router
from app.services.admin_view_catalog_service import list_view_catalog
from app.services.auth_service import get_admin_user


def _build_test_client(admin_ok: bool) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router)

    if admin_ok:
        async def _fake_admin():
            return {"_id": "admin"}
        app.dependency_overrides[get_admin_user] = _fake_admin
    else:
        async def _fake_forbidden():
            raise HTTPException(status_code=403, detail="forbidden")
        app.dependency_overrides[get_admin_user] = _fake_forbidden

    return TestClient(app)


def test_list_view_catalog_summary_matches_items():
    payload = list_view_catalog()
    items = payload["items"]
    summary = payload["summary"]

    assert summary["total"] == len(items)
    assert summary["public"] > 0
    assert summary["admin"] > 0
    assert summary["auth_required"] >= summary["admin_required"]


def test_admin_views_catalog_endpoint_returns_catalog_for_admin():
    client = _build_test_client(admin_ok=True)
    response = client.get("/api/admin/views/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == len(body["items"])
    assert any(item["group"] == "public" for item in body["items"])
    assert any(item["group"] == "admin" for item in body["items"])


def test_admin_views_catalog_endpoint_forbidden_for_non_admin():
    client = _build_test_client(admin_ok=False)
    response = client.get("/api/admin/views/catalog")
    assert response.status_code == 403


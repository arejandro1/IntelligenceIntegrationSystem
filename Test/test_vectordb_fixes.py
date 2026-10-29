# -*- coding: utf-8 -*-
"""VectorDB 管理端点 token 安全模型与 AsyncJobManager 结果上限的回归测试。

对应修复：
- _require_admin_token 只应用于 /api/admin/* 路由，
  token 未配置时数据端点（/api/health 等）不再全部 503；
- AsyncJobManager 单条结果序列化后超过大小上限时丢弃结果，
  只保留状态与错误提示，防止内存不可控。

依赖 chromadb 等重量级包的环境才执行；依赖缺失时整文件 skip。
"""
import time

import pytest

flask = pytest.importorskip("flask")
service_module = pytest.importorskip("VectorDB.VectorDBBService")


def _make_client(token):
    from flask import Flask

    service = service_module.VectorDBService(engine=None)
    app = Flask(__name__)
    wrapper = service_module.VectorDBService._require_admin_token(token)
    service.mount_to_app(app, url_prefix="", wrapper=wrapper)
    return app.test_client()


class TestAdminTokenScope:
    """token 校验只作用于 /api/admin/*，数据端点不受 token 配置影响。"""

    def test_health_ok_when_token_empty(self):
        client = _make_client("")
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_health_ok_when_token_set(self):
        client = _make_client("secret-token")
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_admin_endpoint_fail_closed_when_token_empty(self):
        client = _make_client("")
        resp = client.get("/api/admin/backup")
        assert resp.status_code == 503

    def test_admin_endpoint_rejects_missing_or_wrong_token(self):
        client = _make_client("secret-token")
        assert client.get("/api/admin/backup").status_code == 401
        resp = client.get(
            "/api/admin/backup",
            headers={"X-VectorDB-Admin-Token": "wrong-token"},
        )
        assert resp.status_code == 401

    def test_admin_endpoint_passes_token_gate_with_correct_token(self):
        client = _make_client("secret-token")
        # engine=None 会在通过 token 校验后才报错（500）；
        # 只要不是 401/503，即证明 token 门已放行。
        resp = client.get(
            "/api/admin/backup",
            headers={"X-VectorDB-Admin-Token": "secret-token"},
        )
        assert resp.status_code not in (401, 503)


def _wait_finished(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = mgr.get(job_id, include_result=True)
        if job and job.get("status") in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


class TestAsyncJobResultCap:
    """单条结果超过大小上限时丢弃结果，只留状态与错误提示。"""

    def test_small_result_kept(self):
        mgr = service_module.AsyncJobManager(max_workers=1, max_result_bytes=1024)
        job = mgr.submit("test", lambda: {"ok": True})
        done = _wait_finished(mgr, job["job_id"])
        assert done["status"] == "succeeded"
        assert done["result"] == {"ok": True}
        assert done["error"] is None

    def test_oversized_result_dropped_with_error_note(self):
        mgr = service_module.AsyncJobManager(max_workers=1, max_result_bytes=64)
        job = mgr.submit("test", lambda: {"data": "x" * 1000})
        done = _wait_finished(mgr, job["job_id"])
        assert done["status"] == "succeeded"
        assert done["result"] is None
        assert "too large" in (done["error"] or "")

    def test_result_none_passthrough(self):
        mgr = service_module.AsyncJobManager(max_workers=1, max_result_bytes=1)
        job = mgr.submit("test", lambda: None)
        done = _wait_finished(mgr, job["job_id"])
        assert done["status"] == "succeeded"
        assert done["result"] is None

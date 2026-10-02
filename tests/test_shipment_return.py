"""已装车交接批次退货测试。

覆盖：
- 部分/全部退回：凭原交接号 + 唯一退回号 + 非空原因 + 该交接内非空无重复批次清单
  登记退回；逐批位置引用原交接顺序、内联原交接冻结快照引用；
- 名额占用：未退回批次仍占用发运名额（同单新交接整单 409），全部退回后原单据
  仍不得再次交接（VERIFICATION_ALREADY_HANDED_OVER）；
- 退回后再发运：批次转为可再次发运，但必须纳入新核验单完成当前版本确认；
  新交接仍按有效批次、RELEASE/SHIPPABLE、版本一致逐批校验；
- 每批同一时刻最多一笔未退回交接（跨单占用兜底）；
- 按批次可查历次交接、退回及当前去向；原交接详情仍显示当时完整清单（追加退回标记）；
- 冻结不改写：原交接主记录/逐批快照、发运后复核历史不受退回与后续更正影响；
- 拒绝：重复退回号 409、未知交接 404、非本交接（含未知批次）/已退回批次 422、
  空清单/重复批次/空原因 400/422；失败不写入退回记录、不释放任何占用（原子性）；
- 并发两笔退回同一批次恰有一笔成功；旧库启动幂等补齐退回表。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no="RM-M", material="奶粉", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or ["milk"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lot="RM-M", declared=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": [lot],
            "declared_allergens": ["milk"] if declared is None else declared,
            "cleaned_allergens": [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_handover(client, batches, verification_no="V1", handover_no="H1", lot="RM-M"):
    """登记合格批次 -> 创建核验单 -> 确认 -> 交接；返回 (核验单修订号, 交接响应)。"""
    add_lot(client, lot)
    for bn in batches:
        add_batch(client, bn, lot=lot)
    assert client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batches},
    ).status_code == 201
    doc = client.post(f"/api/verifications/{verification_no}/confirm").json()
    assert doc["status"] == "CONFIRMED"
    resp = client.post(
        "/api/handovers",
        json={
            "handover_no": handover_no,
            "verification_no": verification_no,
            "expected_revision": doc["revision"],
        },
    )
    assert resp.status_code == 201, resp.text
    return doc["revision"], resp.json()


def register_return(client, handover_no, return_no, batch_nos, reason="客户退货"):
    return client.post(
        f"/api/handovers/{handover_no}/returns",
        json={"return_no": return_no, "batch_nos": batch_nos, "reason": reason},
    )


def occupancy(client, batch_no):
    with client.app.state.store.read() as db:
        return db.execute(
            "SELECT handover_no FROM shipment_handover_occupancy WHERE batch_no=?",
            (batch_no,),
        ).fetchone()


# ---------------------------------------------------------------- 成功退回


def test_partial_return_freezes_references_and_releases_only_returned(client):
    _rev, h = make_handover(client, ["B1", "B2", "B3"])

    resp = register_return(client, "H1", "R1", ["B2"], "运输破损退回")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["return_no"] == "R1"
    assert body["handover_no"] == "H1"
    assert body["verification_no"] == "V1"
    assert body["verification_revision"] == 2
    assert body["reason"] == "运输破损退回"
    assert body["batch_count"] == 1
    assert body["returned_at"]
    # 提交清单仅 B2：原交接顺序号引用（B2 在 [B1,B2,B3] 中为 2），内联冻结快照
    assert [b["batch_no"] for b in body["batches"]] == ["B2"]
    item = body["batches"][0]
    assert item["position"] == 2
    assert item["handover_snapshot"]["adjudication"]["version"] == 1
    assert item["handover_snapshot"]["adjudication"]["disposition"] == "RELEASE"
    assert item["handover_snapshot"]["shipment_eligibility"]["version"] == 1
    assert item["handover_snapshot"]["shipment_eligibility"]["status"] == "SHIPPABLE"

    # 仅 B2 释放；B1/B3 仍占用 H1 名额
    assert occupancy(client, "B2") is None
    assert occupancy(client, "B1")["handover_no"] == "H1"
    assert occupancy(client, "B3")["handover_no"] == "H1"

    # B2 追溯：当前未占用，handovers 仍记录 H1 且标注已退回
    trace = client.get("/api/batches/B2/handover").json()
    assert trace["shipped"] is False
    assert trace["handover"] is None
    assert trace["current_whereabouts"]["state"] == "AVAILABLE"
    assert len(trace["handovers"]) == 1
    entry = trace["handovers"][0]
    assert entry["handover_no"] == "H1" and entry["returned"] is True
    assert entry["return"]["return_no"] == "R1"
    assert entry["return"]["reason"] == "运输破损退回"

    # 未退回批次的归属不变
    t1 = client.get("/api/batches/B1/handover").json()
    assert t1["shipped"] is True and t1["handover"]["handover_no"] == "H1"


def test_return_members_reference_original_positions(client):
    make_handover(client, ["B1", "B2", "B3"])
    # 乱序提交 [B3, B1]：逐批条目仍按原交接清单顺序展示，position 引用各自原顺序号
    resp = register_return(client, "H1", "R1", ["B3", "B1"], "整托退回")
    assert resp.status_code == 201
    assert [(b["batch_no"], b["position"]) for b in resp.json()["batches"]] == [
        ("B1", 1),
        ("B3", 3),
    ]


def test_multiple_partial_returns_then_full(client):
    make_handover(client, ["B1", "B2", "B3"])
    assert register_return(client, "H1", "R1", ["B2"], "r").status_code == 201
    resp = register_return(client, "H1", "R2", ["B1", "B3"], "r")
    assert resp.status_code == 201
    assert [(b["batch_no"], b["position"]) for b in resp.json()["batches"]] == [
        ("B1", 1),
        ("B3", 3),
    ]
    # 全部释放
    for bn in ("B1", "B2", "B3"):
        assert occupancy(client, bn) is None

    h = client.get("/api/handovers/H1").json()
    assert h["batch_count"] == 3
    assert h["returned_batch_count"] == 3
    assert h["outstanding_batch_count"] == 0
    assert [r["return_no"] for r in h["returns"]] == ["R1", "R2"]
    # 原交接详情仍是当时完整清单（3 批），逐批追加退回标记
    assert [b["batch_no"] for b in h["batches"]] == ["B1", "B2", "B3"]
    assert all(b["returned"] for b in h["batches"])
    by_no = {b["batch_no"]: b for b in h["batches"]}
    assert by_no["B2"]["return"]["return_no"] == "R1"
    assert by_no["B1"]["return"]["return_no"] == "R2"
    assert by_no["B3"]["return"]["return_no"] == "R2"


# ---------------------------------------------------------------- 名额占用与再发运


def test_unreturned_batches_keep_quota_same_verification_rejected(client):
    make_handover(client, ["B1", "B2"])
    register_return(client, "H1", "R1", ["B1"], "r")

    # B2 未退回仍占用：同一张单据再次交接被占用规则整单拦截（B2 已发运）
    resp = client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V1", "expected_revision": 2},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_ALREADY_SHIPPED"
    assert resp.json()["error"]["details"][0]["batch_no"] == "B2"


def test_same_verification_cannot_rehandover_after_full_return(client):
    make_handover(client, ["B1", "B2"])
    assert register_return(client, "H1", "R1", ["B1", "B2"], "r").status_code == 201

    resp = client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V1", "expected_revision": 2},
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_ALREADY_HANDED_OVER"
    assert err["details"][0]["handover_no"] == "H1"


def test_returned_batch_reshippable_only_via_new_verification_with_current_snapshot(client):
    make_handover(client, ["B1", "B2"])
    register_return(client, "H1", "R1", ["B1"], "r")

    # 新核验单包含已退回 B1 + 未退回 B2：B2 占用 -> 新交接仍整单 409，B1 不被部分占用
    assert client.post(
        "/api/verifications", json={"verification_no": "V2", "batch_nos": ["B1", "B2"]}
    ).status_code == 201
    client.post("/api/verifications/V2/confirm")
    resp = client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V2", "expected_revision": 2},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_ALREADY_SHIPPED"
    assert occupancy(client, "B1") is None  # 失败交接不占用已释放的 B1

    # 仅 B1 的新核验单：确认 + 交接成功
    assert client.post(
        "/api/verifications", json={"verification_no": "V3", "batch_nos": ["B1"]}
    ).status_code == 201
    client.post("/api/verifications/V3/confirm")
    resp = client.post(
        "/api/handovers",
        json={"handover_no": "H3", "verification_no": "V3", "expected_revision": 2},
    )
    assert resp.status_code == 201, resp.text
    assert occupancy(client, "B1")["handover_no"] == "H3"

    # 按批次可查历次交接与退回：H1(已退回 R1) -> H3(未退回，当前去向)
    trace = client.get("/api/batches/B1/handover").json()
    assert trace["shipped"] is True
    assert trace["handover"]["handover_no"] == "H3"
    assert [(h["handover_no"], h["returned"]) for h in trace["handovers"]] == [
        ("H1", True),
        ("H3", False),
    ]
    assert trace["current_whereabouts"]["state"] == "SHIPPED"
    assert trace["current_whereabouts"]["handover_no"] == "H3"


def test_returned_batch_must_reconfirm_current_version_after_drift(client):
    """退回后产生新版本：旧确认无法复用，新核验单确认按当前版本门禁。"""
    make_handover(client, ["B1"])
    register_return(client, "H1", "R1", ["B1"], "r")

    # 退回后更正 B1 为 HOLD（裁决 v2）
    r = client.put("/api/batches/B1", json={"declared_allergens": []})
    assert r.status_code == 200
    b1 = next(x for x in r.json()["recomputed"] if x["batch_no"] == "B1")
    assert b1["disposition"] == "HOLD" and b1["version"] == 2

    # 新核验单含 HOLD 批次：确认 422（退回不豁免放行门禁）
    client.post("/api/verifications", json={"verification_no": "V2", "batch_nos": ["B1"]})
    r = client.post("/api/verifications/V2/confirm")
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["reason_code"] == "BATCH_ON_HOLD"
    assert occupancy(client, "B1") is None

    # 恢复 RELEASE（v3）后，新核验单以当前版本确认、交接成功
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    client.post("/api/verifications", json={"verification_no": "V3", "batch_nos": ["B1"]})
    doc = client.post("/api/verifications/V3/confirm").json()
    assert doc["batches"][0]["snapshot"]["version"] == 3
    r = client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V3", "expected_revision": doc["revision"]},
    )
    assert r.status_code == 201
    assert r.json()["batches"][0]["adjudication"]["version"] == 3


def test_returned_batch_with_locked_lot_cannot_confirm(client):
    make_handover(client, ["B1"])
    register_return(client, "H1", "R1", ["B1"], "r")

    # 退回后原料批号待检锁定：出货资格 BLOCKED，新核验单确认拦截
    r = client.post("/api/material-lots/RM-M/lock", json={"reason": "待检风险"})
    assert r.status_code == 200
    client.post("/api/verifications", json={"verification_no": "V2", "batch_nos": ["B1"]})
    r = client.post("/api/verifications/V2/confirm")
    assert r.status_code == 422
    codes = {d.get("reason_code") for d in r.json()["error"]["details"]}
    assert "MATERIAL_LOT_LOCKED" in codes


def test_at_most_one_outstanding_handover_per_batch_across_handovers(client):
    """同一批次在 H1 退回、H2 再发运、H2 未退回期间，任何其他交接不得占用。"""
    make_handover(client, ["B1"])
    register_return(client, "H1", "R1", ["B1"], "r")
    # 第二批产线，另一批 B9 与 B1 组单
    add_batch(client, "B9", line_id="L2")
    client.post("/api/verifications", json={"verification_no": "V2", "batch_nos": ["B1"]})
    client.post("/api/verifications/V2/confirm")
    assert client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V2", "expected_revision": 2},
    ).status_code == 201

    client.post("/api/verifications", json={"verification_no": "V9", "batch_nos": ["B1", "B9"]})
    client.post("/api/verifications/V9/confirm")
    r = client.post(
        "/api/handovers",
        json={"handover_no": "H9", "verification_no": "V9", "expected_revision": 2},
    )
    assert r.status_code == 409
    assert r.json()["error"]["details"][0]["batch_no"] == "B1"
    assert r.json()["error"]["details"][0]["handover_no"] == "H2"
    # B9 未被部分占用
    assert occupancy(client, "B9") is None


# ---------------------------------------------------------------- 冻结不改写


def test_handover_and_review_history_immutable_after_return(client):
    make_handover(client, ["B1"])
    register_return(client, "H1", "R1", ["B1"], "r")

    # 退回后更正 B1（新版本）：原交接冻结快照不变；退回记录内联引用同样不变
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    h = client.get("/api/handovers/H1").json()
    assert h["verification_revision"] == 2
    assert h["batches"][0]["adjudication"]["version"] == 1
    assert h["batches"][0]["adjudication"]["disposition"] == "RELEASE"

    r1 = client.get("/api/returns/R1").json()
    assert r1["batches"][0]["handover_snapshot"]["adjudication"]["version"] == 1

    # 发运后复核仍以原交接冻结快照为基准：版本漂移形成待复核事项，处置历史可继续追加
    board = client.get("/api/handovers/H1/reviews").json()
    item = board["batches"][0]
    assert item["review_status"] == "PENDING_REVIEW"
    seen = item["seen_state"]
    r = client.post(
        "/api/handovers/H1/reviews/B1/decision",
        json={
            "seen_adjudication_version": seen["adjudication_version"],
            "seen_shipment_version": seen["shipment_version"],
            "seen_batch_voided": seen["batch_voided"],
            "disposition": "RECALL_REQUIRED",
            "reason": "交接后更正，退回后仍需评估召回",
        },
    )
    assert r.status_code == 201, r.text
    board = client.get("/api/handovers/H1/reviews").json()
    assert board["summary"]["reviewed"] == 1
    # 退回记录不携带/不改写复核；复核历史按交接仍可查
    detail = client.get("/api/handovers/H1").json()
    assert detail["batches"][0]["return"]["return_no"] == "R1"


def test_voided_batch_can_be_returned_but_cannot_reshipped(client):
    """已发运批次作废后仍可登记退回（释放占用），但作废批次不能再纳入新核验单。"""
    make_handover(client, ["B1"])
    r = client.post("/api/batches/B1/void", json={"reason": "误登记未投产"})
    assert r.status_code == 200
    resp = register_return(client, "H1", "R1", ["B1"], "作废产品退回仓库")
    assert resp.status_code == 201, resp.text
    assert occupancy(client, "B1") is None
    # 作废批次不能加入新核验单（既有规则）
    r = client.post("/api/verifications", json={"verification_no": "V2", "batch_nos": ["B1"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BATCH_VOIDED"


# ---------------------------------------------------------------- 查询接口


def test_list_and_get_returns(client):
    make_handover(client, ["B1", "B2"])
    register_return(client, "H1", "R1", ["B1"], "r1")
    register_return(client, "H1", "R2", ["B2"], "r2")

    # 按原交接过滤
    body = client.get("/api/handovers/H1/returns").json()
    assert [r["return_no"] for r in body["returns"]] == ["R1", "R2"]
    assert body["returns"][0]["batches"] == [{"position": 1, "batch_no": "B1"}]

    # 全量列表
    body = client.get("/api/returns").json()
    assert [r["return_no"] for r in body["returns"]] == ["R1", "R2"]
    # query 参数过滤
    body = client.get("/api/returns?handover_no=H1").json()
    assert len(body["returns"]) == 2

    # 详情
    d = client.get("/api/returns/R2").json()
    assert d["handover_no"] == "H1" and d["batch_count"] == 1
    assert d["batches"][0]["batch_no"] == "B2" and d["batches"][0]["position"] == 2

    # 404
    assert client.get("/api/returns/NOPE").status_code == 404
    assert client.get("/api/handovers/NOPE/returns").status_code == 404
    assert client.get("/api/returns?handover_no=NOPE").status_code == 404


def test_handover_list_summary_carries_returns(client):
    make_handover(client, ["B1", "B2"])
    register_return(client, "H1", "R1", ["B1"], "r")
    body = client.get("/api/handovers").json()["handovers"]
    h = next(x for x in body if x["handover_no"] == "H1")
    assert h["returned_batch_count"] == 1
    assert h["outstanding_batch_count"] == 1
    assert h["returns"][0]["return_no"] == "R1"
    assert h["returns"][0]["batches"] == [{"position": 1, "batch_no": "B1"}]


# ---------------------------------------------------------------- 拒绝路径


def test_duplicate_return_no_rejected_without_partial_release(client):
    make_handover(client, ["B1", "B2"])
    assert register_return(client, "H1", "R1", ["B1"], "r").status_code == 201

    # 同号再退（即使换交接，也被全局唯一拦截）
    resp = register_return(client, "H1", "R1", ["B2"], "r")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_RETURN"
    # B2 未被释放
    assert occupancy(client, "B2")["handover_no"] == "H1"
    # 没有第二笔退回记录
    with client.app.state.store.read() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM shipment_return").fetchone()["n"]
    assert n == 1


def test_unknown_handover_404(client):
    resp = register_return(client, "NOPE", "R1", ["B1"], "r")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HANDOVER_NOT_FOUND"
    assert client.get("/api/returns/R1").status_code == 404


def test_batch_not_in_handover_and_already_returned_collected_atomically(client):
    make_handover(client, ["B1", "B2"])
    add_lot(client, "RM-OTHER")
    add_batch(client, "B9", lot="RM-OTHER")  # 已登记但不属于 H1
    register_return(client, "H1", "R1", ["B1"], "r")

    resp = register_return(client, "H1", "R2", ["B1", "B9", "GHOST"], "r")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "RETURN_NOT_ALLOWED"
    by_batch = {d["batch_no"]: d["reason_code"] for d in err["details"]}
    assert by_batch == {
        "B1": "BATCH_ALREADY_RETURNED",
        "B9": "BATCH_NOT_IN_HANDOVER",
        "GHOST": "BATCH_NOT_IN_HANDOVER",
    }
    # 已退回 B1 的详情指向 R1
    b1 = next(d for d in err["details"] if d["batch_no"] == "B1")
    assert b1["return_no"] == "R1"
    # 整体拒绝：R2 不存在，任何占用都未变动（B2 仍占用）
    assert client.get("/api/returns/R2").status_code == 404
    assert occupancy(client, "B2")["handover_no"] == "H1"
    with client.app.state.store.read() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_return_batch WHERE return_no='R2'"
        ).fetchone()["n"] == 0


def test_invalid_payloads(client):
    make_handover(client, ["B1"])
    base = "/api/handovers/H1/returns"
    # 空白原因 / 空白退回号
    for payload in [
        {"return_no": "R1", "batch_nos": ["B1"], "reason": "   "},
        {"return_no": "  ", "batch_nos": ["B1"], "reason": "r"},
        {"return_no": "R1", "batch_nos": ["  "], "reason": "r"},
    ]:
        r = client.post(base, json=payload)
        assert r.status_code == 400, (payload, r.text)
        assert r.json()["error"]["code"] == "INVALID_PAYLOAD"
    # 清单内重复
    r = client.post(base, json={"return_no": "R1", "batch_nos": ["B1", "B1"], "reason": "r"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "DUPLICATE_BATCH_IN_RETURN"
    # 空清单/缺字段 -> pydantic 422
    assert client.post(base, json={"return_no": "R1", "batch_nos": [], "reason": "r"}).status_code == 422
    assert client.post(base, json={"return_no": "R1", "reason": "r"}).status_code == 422
    # 全部失败后没有任何退回记录、占用不变
    with client.app.state.store.read() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM shipment_return").fetchone()["n"] == 0
    assert occupancy(client, "B1")["handover_no"] == "H1"


def test_failed_return_leaves_no_rows(client):
    make_handover(client, ["B1", "B2"])
    register_return(client, "H1", "R1", ["B1"], "r")
    # 含已退回批次 -> 整体失败：主记录/逐批引用/占用删除均不得发生
    resp = register_return(client, "H1", "R2", ["B1", "B2"], "r")
    assert resp.status_code == 422
    store = client.app.state.store
    with store.read() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_return WHERE return_no='R2'"
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_return_batch WHERE return_no='R2'"
        ).fetchone()["n"] == 0
        # B2 占用仍在（未发生部分释放）
        assert db.execute(
            "SELECT handover_no FROM shipment_handover_occupancy WHERE batch_no='B2'"
        ).fetchone()["handover_no"] == "H1"
        # B1 占用早在 R1 成功时已删除，不应被重新插入
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_handover_occupancy WHERE batch_no='B1'"
        ).fetchone()["n"] == 0


# ---------------------------------------------------------------- 并发原子性


def test_concurrent_returns_same_batch_exactly_one_succeeds(client, monkeypatch):
    """两笔退回同时包含 B1：写事务串行，恰有一笔成功；失败方不释放任何批次。"""
    make_handover(client, ["B1", "B2"])
    service = client.app.state.service
    store = client.app.state.store
    rlock = store._write_lock
    raw = store._conn
    inside = threading.Event()
    release = threading.Event()
    hold = {"on": True}

    @contextmanager
    def instrumented_write():
        with rlock:
            if hold["on"]:
                hold["on"] = False
                inside.set()
                release.wait(timeout=10)
            raw.execute("BEGIN IMMEDIATE")
            try:
                yield raw
                raw.execute("COMMIT")
            except Exception:
                try:
                    raw.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    monkeypatch.setattr(store, "write", instrumented_write)
    results: dict[str, tuple] = {}

    def run(key, return_no, batches):
        try:
            out = service.register_return("H1", return_no, batches, "并发退回")
            results[key] = (201, out["return_no"])
        except Exception as exc:  # APIError
            results[key] = (exc.status_code, exc.code)

    t1 = threading.Thread(target=run, args=("a", "R1", ["B1"]))
    t1.start()
    inside.wait(timeout=5)
    t2 = threading.Thread(target=run, args=("b", "R2", ["B1", "B2"]))
    t2.start()
    t2.join(timeout=0.5)
    assert t2.is_alive()  # 串行等待写锁
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    statuses = {k: v[0] for k, v in results.items()}
    assert sorted(statuses.values()) == [201, 422], results
    failed_key = "a" if statuses["a"] == 422 else "b"
    assert results[failed_key][1] == "RETURN_NOT_ALLOWED"

    # 成功方释放其批次；失败方不释放任何批次（B2 一定仍占用 H1）
    assert occupancy(client, "B2")["handover_no"] == "H1"
    with store.read() as db:
        returned = {
            r["batch_no"]: r["return_no"]
            for r in db.execute(
                "SELECT batch_no, return_no FROM shipment_return_batch"
            ).fetchall()
        }
    assert returned == {"B1": "R1"}


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_without_return_tables_upgrades_and_works(tmp_path):
    """无退回表的旧库（含交接表）启动后幂等建表，可完成交接后退回。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen (code TEXT PRIMARY KEY, name_zh TEXT NOT NULL);
        CREATE TABLE material_lot (lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE line (line_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE batch (batch_no TEXT PRIMARY KEY, line_id TEXT NOT NULL,
            seq INTEGER NOT NULL, material_lots TEXT NOT NULL DEFAULT '[]',
            declared TEXT NOT NULL DEFAULT '[]', cleaned TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'ACTIVE', voided_reason TEXT, voided_at TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE batch_material (batch_no TEXT NOT NULL, lot_no TEXT NOT NULL,
            PRIMARY KEY (batch_no, lot_no));
        CREATE TABLE material_lot_lock (lot_no TEXT PRIMARY KEY, locked INTEGER NOT NULL,
            reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_no TEXT NOT NULL, version INTEGER NOT NULL, locked INTEGER NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, locked_lots TEXT NOT NULL DEFAULT '[]',
            reasons TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
            locked_lots TEXT NOT NULL DEFAULT '[]', reasons TEXT NOT NULL DEFAULT '[]',
            trigger TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE adjudication (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
            actual TEXT NOT NULL DEFAULT '[]', undeclared TEXT NOT NULL DEFAULT '[]',
            disposition TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
            sources TEXT NOT NULL DEFAULT '{}', decided_at TEXT NOT NULL);
        CREATE TABLE adjudication_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, line_id TEXT NOT NULL, seq INTEGER NOT NULL,
            version INTEGER NOT NULL, actual TEXT NOT NULL DEFAULT '[]',
            undeclared TEXT NOT NULL DEFAULT '[]', disposition TEXT NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '{}',
            trigger TEXT NOT NULL, decided_at TEXT NOT NULL);
        CREATE TABLE verification (verification_no TEXT PRIMARY KEY, status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE verification_batch (verification_no TEXT NOT NULL, batch_no TEXT NOT NULL,
            position INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]', snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER, snapshot_ship_status TEXT,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]', snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]', snapshot_ship_version INTEGER,
            snapshot_ship_status TEXT, snapshot_locked_lots TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE shipment_handover (handover_no TEXT PRIMARY KEY, verification_no TEXT NOT NULL,
            verification_revision INTEGER NOT NULL, confirmation_id INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL, batch_count INTEGER NOT NULL, handed_over_at TEXT NOT NULL);
        CREATE TABLE shipment_handover_batch (handover_no TEXT NOT NULL, position INTEGER NOT NULL,
            batch_no TEXT NOT NULL, confirmation_id INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]', snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER NOT NULL, snapshot_ship_status TEXT NOT NULL,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]', snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (handover_no, batch_no));
        CREATE TABLE shipment_handover_occupancy (batch_no TEXT PRIMARY KEY, handover_no TEXT NOT NULL,
            position INTEGER NOT NULL, handed_over_at TEXT NOT NULL);
        CREATE TABLE shipment_review (id INTEGER PRIMARY KEY AUTOINCREMENT, handover_no TEXT NOT NULL,
            batch_no TEXT NOT NULL, position INTEGER NOT NULL, seen_adjudication_version INTEGER NOT NULL,
            seen_shipment_version INTEGER NOT NULL, seen_batch_voided INTEGER NOT NULL,
            disposition TEXT NOT NULL, reason TEXT NOT NULL, reviewed_at TEXT NOT NULL);
        INSERT INTO allergen(code,name_zh) VALUES ('milk','乳');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        store = app.state.store
        with store.read() as db:
            names = {
                r["name"]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"shipment_return", "shipment_return_batch"} <= names

        add_lot(c)
        add_batch(c, "B1")
        c.post("/api/verifications", json={"verification_no": "V1", "batch_nos": ["B1"]})
        c.post("/api/verifications/V1/confirm")
        assert c.post(
            "/api/handovers",
            json={"handover_no": "H1", "verification_no": "V1", "expected_revision": 2},
        ).status_code == 201

        r = register_return(c, "H1", "R1", ["B1"], "旧库升级后退货")
        assert r.status_code == 201, r.text
        d = r.json()
        assert d["batches"][0]["handover_snapshot"]["adjudication"]["version"] == 1
        assert c.get("/api/batches/B1/handover").json()["shipped"] is False

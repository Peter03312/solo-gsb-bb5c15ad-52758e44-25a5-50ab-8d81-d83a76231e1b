"""交接退回（已装车交接批次退货登记）测试。

覆盖：
- 成功退回：凭唯一退回号 + 原交接号 + 非空原因 + 交接内非空无重复批次清单登记；
  退回记录冻结原交接单号/修订号/确认引用与逐批交接快照副本（保序）；
- 部分退回：未退回批次仍占用发运名额，退回批次释放占用、转为可再次发运；
- 再次发运：退回批次纳入新核验单完成当前版本确认后，经新交接发运；
  新交接仍按现有有效批次/放行/出货资格/版本规则逐批校验；
- 不可变：原交接记录（单号/修订号/逐批冻结快照/当时完整清单）与发运后复核历史
  均不改写；交接详情仍显示当时完整清单并标注退回；
- 拒绝（整体回滚、不留下部分释放）：重复退回号 409、未知交接 404、空原因 400、
  空清单 422/400、清单内重复批次 400、非本交接批次 404、已退回批次 409；
- 追溯：按批次查历次交接、退回及当前去向（每批同一时刻最多一笔未退回交接）；
- 不影响核验单状态/修订号、裁决/出货资格版本；
- 原子性：失败路径不写退回记录、不释放任何占用；并发退回同一批恰一笔成功；
- 旧库升级：无退回表的旧库启动后幂等建表，既有交接可登记退回。
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


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


def confirm(client, verification_no):
    resp = client.post(f"/api/verifications/{verification_no}/confirm")
    assert resp.status_code == 200, resp.text
    return resp.json()


def handover(client, handover_no, verification_no, expected_revision=2):
    resp = client.post(
        "/api/handovers",
        json={
            "handover_no": handover_no,
            "verification_no": verification_no,
            "expected_revision": expected_revision,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def do_return(client, return_no, handover_no, batch_nos, reason="客户拒收"):
    return client.post(
        "/api/returns",
        json={
            "return_no": return_no,
            "handover_no": handover_no,
            "reason": reason,
            "batch_nos": batch_nos,
        },
    )


def release_batches(client, *batch_nos, line_id="L1", lot="RM-M"):
    """登记一个含 milk 的批号与若干全部 RELEASE/SHIPPABLE 的批次（逐批声明 milk）。"""
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot, "material": "奶粉", "allergens": ["milk"]},
    )
    assert resp.status_code in (201, 409), resp.text  # 批号已存在则复用
    for bn in batch_nos:
        add_batch(client, bn, line_id=line_id, lots=[lot], declared=["milk"])


def shipped_batch(client, batch_no, verification_no, handover_no):
    """登记批次 -> 核验单 -> 确认 -> 交接，返回交接响应体。"""
    release_batches(client, batch_no)
    create_verification(client, verification_no, [batch_no])
    confirm(client, verification_no)
    return handover(client, handover_no, verification_no)


def _return_rows(client, return_no):
    store = client.app.state.store
    with store.read() as db:
        main = db.execute(
            "SELECT COUNT(*) AS n FROM shipment_return WHERE return_no=?",
            (return_no,),
        ).fetchone()["n"]
        batches = db.execute(
            "SELECT COUNT(*) AS n FROM shipment_return_batch WHERE return_no=?",
            (return_no,),
        ).fetchone()["n"]
    return main, batches


def _occupancy(client, batch_no):
    store = client.app.state.store
    with store.read() as db:
        row = db.execute(
            "SELECT handover_no FROM shipment_handover_occupancy WHERE batch_no=?",
            (batch_no,),
        ).fetchone()
    return None if row is None else row["handover_no"]


# ---------------------------------------------------------------- 成功退回（部分/全部）


def test_partial_return_freezes_reference_and_releases_only_returned(client):
    release_batches(client, "B1", "B2", "B3")
    create_verification(client, "V1", ["B1", "B2", "B3"])
    doc = confirm(client, "V1")
    ho = handover(client, "H1", "V1", doc["revision"])

    resp = do_return(client, "RT1", "H1", ["B2", "B1"], reason="客户拒收，外包装破损")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["return_no"] == "RT1"
    assert body["handover_no"] == "H1"
    # 冻结原交接引用
    assert body["verification_no"] == "V1"
    assert body["verification_revision"] == doc["revision"]
    assert body["confirmation_id"] == ho["confirmation_id"]
    assert body["reason"] == "客户拒收，外包装破损"
    assert body["batch_count"] == 2
    assert body["returned_at"] is not None
    # 逐批冻结交接快照副本，按退回清单顺序，并记录原交接清单位置
    assert [b["batch_no"] for b in body["batches"]] == ["B2", "B1"]
    assert [b["position"] for b in body["batches"]] == [1, 2]
    assert [b["handover_position"] for b in body["batches"]] == [2, 1]
    for b in body["batches"]:
        assert b["confirmation_id"] == ho["confirmation_id"]
        assert b["adjudication"]["version"] == 1
        assert b["adjudication"]["disposition"] == "RELEASE"
        assert b["shipment_eligibility"]["version"] == 1
        assert b["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert body["released"] == [
        {"batch_no": "B2", "handover_no": "H1"},
        {"batch_no": "B1", "handover_no": "H1"},
    ]

    # 退回批次释放占用；未退回批次 B3 仍占用发运名额
    assert _occupancy(client, "B1") is None
    assert _occupancy(client, "B2") is None
    assert _occupancy(client, "B3") == "H1"
    assert client.get("/api/batches/B1/handover").json()["shipped"] is False
    assert client.get("/api/batches/B3/handover").json()["shipped"] is True


def test_full_return_marks_handover_fully_returned(client):
    shipped_batch(client, "B1", "V1", "H1")
    resp = do_return(client, "RT1", "H1", ["B1"])
    assert resp.status_code == 201, resp.text

    detail = client.get("/api/handovers/H1").json()
    assert detail["returned_batch_count"] == 1
    assert detail["fully_returned"] is True
    assert [r["return_no"] for r in detail["returns"]] == ["RT1"]


def test_handover_detail_keeps_original_full_list_with_return_marks(client):
    """原有交接详情仍显示当时完整清单；退回批次以 returned/return 标注。"""
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B2", "B1"])
    confirm(client, "V1")
    handover(client, "H1", "V1")
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    detail = client.get("/api/handovers/H1").json()
    # 当时完整清单不变（含已退回批次），逐批冻结快照不改写

    assert [b["batch_no"] for b in detail["batches"]] == ["B2", "B1"]
    assert detail["batch_count"] == 2
    by_no = {b["batch_no"]: b for b in detail["batches"]}
    assert by_no["B1"]["returned"] is True
    assert by_no["B1"]["return"]["return_no"] == "RT1"
    assert by_no["B1"]["return"]["reason"] == "客户拒收"
    assert by_no["B2"]["returned"] is False
    assert by_no["B2"]["return"] is None
    for b in detail["batches"]:
        assert b["adjudication"]["version"] == 1
        assert b["adjudication"]["disposition"] == "RELEASE"
    assert detail["returned_batch_count"] == 1
    assert detail["fully_returned"] is False
    assert detail["returns"][0]["reason"] == "客户拒收"

    # 列表摘要同样保留成员并给出退回计数
    summaries = client.get("/api/handovers").json()["handovers"]
    assert summaries[0]["batches"] == [
        {"position": 1, "batch_no": "B2"},
        {"position": 2, "batch_no": "B1"},
    ]
    assert summaries[0]["returned_batch_count"] == 1


# ---------------------------------------------------------------- 退回批次再次发运


def test_returned_batch_can_ship_again_via_new_verification(client):
    """退回批次纳入新核验单完成当前版本确认后，可经新交接再次发运。"""
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1", "B2"])
    confirm(client, "V1")
    handover(client, "H1", "V1")
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    # 未退回的 B2 仍被 H1 占用：含 B2 的新核验单交接仍整单拒绝
    create_verification(client, "V2", ["B2"])
    d2 = confirm(client, "V2")
    resp = client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V2",
              "expected_revision": d2["revision"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_ALREADY_SHIPPED"

    # 退回的 B1 纳入新核验单 -> 确认 -> 新交接成功
    create_verification(client, "V3", ["B1"])
    d3 = confirm(client, "V3")
    ho2 = handover(client, "H3", "V3", d3["revision"])
    assert ho2["batches"][0]["batch_no"] == "B1"
    assert _occupancy(client, "B1") == "H3"

    # 每批同一时刻最多一笔未退回交接；历次交接与退回均可查
    trace = client.get("/api/batches/B1/handovers").json()
    assert trace["shipped"] is True
    assert trace["current"]["handover_no"] == "H3"
    assert [h["handover_no"] for h in trace["handovers"]] == ["H1", "H3"]
    assert trace["handovers"][0]["returned"] is True
    assert trace["handovers"][0]["return"]["return_no"] == "RT1"
    assert trace["handovers"][1]["returned"] is False
    assert trace["handovers"][1]["return"] is None


def test_returned_batch_reshipment_still_enforces_version_rules(client):
    """退回后再发运仍按现有规则逐批校验：更正后须重新核验确认才能交接。"""
    shipped_batch(client, "B1", "V1", "H1")
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    # 退回后批次被更正（声明新增 egg）：产生新裁决版本
    r = client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    assert r.status_code == 200

    # 新核验单创建即冻结当前版本 v2，确认后交接成功（当前版本确认）
    create_verification(client, "V2", ["B1"])
    doc = client.get("/api/verifications/V2").json()
    assert doc["batches"][0]["snapshot"]["version"] == 2
    d2 = confirm(client, "V2")
    assert handover(client, "H2", "V2", d2["revision"])["batch_count"] == 1


def test_return_does_not_touch_verification_or_versions(client):
    """退回不改写核验单状态/修订号，也不推进裁决/出货资格版本。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    doc = confirm(client, "V1")
    handover(client, "H1", "V1", doc["revision"])

    before = client.get("/api/verifications/V1").json()
    adj_before = client.get("/api/batches/B1").json()["adjudication"]["version"]
    ship_before = client.get("/api/batches/B1").json()["shipment_eligibility"]["version"]

    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    after = client.get("/api/verifications/V1").json()
    assert after["status"] == before["status"] == "CONFIRMED"
    assert after["revision"] == before["revision"]
    assert after["updated_at"] == before["updated_at"]
    b = client.get("/api/batches/B1").json()
    assert b["adjudication"]["version"] == adj_before
    assert b["shipment_eligibility"]["version"] == ship_before


def test_return_does_not_rewrite_handover_or_review_history(client):
    """原交接记录与发运后复核历史均不可改写。"""
    shipped_batch(client, "B1", "V1", "H1")
    # 交接后更正 -> 形成发运后复核事项并处置
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    board = client.get("/api/handovers/H1/reviews").json()
    seen = board["batches"][0]["seen_state"]
    resp = client.post(
        "/api/handovers/H1/reviews/B1/decision",
        json={
            "seen_adjudication_version": seen["adjudication_version"],
            "seen_shipment_version": seen["shipment_version"],
            "seen_batch_voided": seen["batch_voided"],
            "disposition": "NO_RECALL",
            "reason": "仅为标签声明补充，无召回风险",
        },
    )
    assert resp.status_code == 201, resp.text

    ho_before = client.get("/api/handovers/H1").json()
    reviews_before = client.get("/api/handovers/H1/reviews").json()

    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    ho_after = client.get("/api/handovers/H1").json()
    # 交接冻结字段与逐批快照不变（仅追加退回标注）
    for key in ("verification_no", "verification_revision", "confirmation_id",
                "confirmed_at", "batch_count", "handed_over_at"):
        assert ho_after[key] == ho_before[key]
    assert ho_after["batches"][0]["adjudication"] == ho_before["batches"][0]["adjudication"]
    assert ho_after["batches"][0]["shipment_eligibility"] == (
        ho_before["batches"][0]["shipment_eligibility"]
    )
    # 复核历史只追加不改写：处置记录与复核状态保持
    reviews_after = client.get("/api/handovers/H1/reviews").json()
    assert reviews_after["batches"][0]["reviews"] == reviews_before["batches"][0]["reviews"]
    assert reviews_after["batches"][0]["review_status"] == "REVIEWED"
    assert reviews_after["batches"][0]["handover_snapshot"] == (
        reviews_before["batches"][0]["handover_snapshot"]
    )


# ---------------------------------------------------------------- 拒绝场景（整体回滚）


def test_duplicate_return_no_rejected(client):
    shipped_batch(client, "B1", "V1", "H1")
    shipped_batch(client, "B2", "V2", "H2")
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    resp = do_return(client, "RT1", "H2", ["B2"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_RETURN"
    # 不留下部分释放：B2 仍被 H2 占用，且无退回记录写入
    assert _occupancy(client, "B2") == "H2"
    assert client.get("/api/returns?handover_no=H2").json()["returns"] == []
    assert _return_rows(client, "RT1") == (1, 1)  # 仅首次退回的一主一从


def test_unknown_handover_404(client):
    resp = do_return(client, "RT1", "NOPE", ["B1"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HANDOVER_NOT_FOUND"
    assert client.get("/api/returns/RT1").status_code == 404


def test_blank_reason_and_empty_fields_400(client):
    shipped_batch(client, "B1", "V1", "H1")
    for payload in [
        {"return_no": "RT1", "handover_no": "H1", "reason": "   ", "batch_nos": ["B1"]},
        {"return_no": "  ", "handover_no": "H1", "reason": "x", "batch_nos": ["B1"]},
        {"return_no": "RT1", "handover_no": " ", "reason": "x", "batch_nos": ["B1"]},
    ]:
        resp = client.post("/api/returns", json=payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    assert _occupancy(client, "B1") == "H1"


def test_empty_or_invalid_batch_list_rejected(client):
    shipped_batch(client, "B1", "V1", "H1")
    # 空清单 -> pydantic 422
    resp = do_return(client, "RT1", "H1", [])
    assert resp.status_code == 422
    # 清单元素空白 -> 400
    resp = do_return(client, "RT1", "H1", ["  "])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    # 清单内重复 -> 400
    resp = do_return(client, "RT1", "H1", ["B1", "B1"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH_IN_RETURN"
    assert _occupancy(client, "B1") == "H1"
    assert _return_rows(client, "RT1") == (0, 0)


def test_batch_not_in_handover_404_and_no_partial_release(client):
    """非本交接批次整体拒绝：即使清单中其余批次合法，也不留下部分释放。"""
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1"])
    create_verification(client, "V2", ["B2"])
    confirm(client, "V1")
    confirm(client, "V2")
    handover(client, "H1", "V1")
    handover(client, "H2", "V2")

    resp = do_return(client, "RT1", "H1", ["B1", "B2"])  # B2 属于 H2
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "BATCH_NOT_IN_HANDOVER"
    assert err["details"][0]["batch_no"] == "B2"
    assert err["details"][0]["handover_no"] == "H1"
    # 合法批 B1 也不被部分释放
    assert _occupancy(client, "B1") == "H1"
    assert _occupancy(client, "B2") == "H2"
    assert _return_rows(client, "RT1") == (0, 0)

    # 未知批次同样按“非本交接”拒绝
    resp = do_return(client, "RT1", "H1", ["GHOST"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_IN_HANDOVER"


def test_already_returned_batch_409_and_no_partial_release(client):
    """已退回批次再次退回（含混在清单中）整体拒绝，不留下部分释放。"""
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1", "B2"])
    confirm(client, "V1")
    handover(client, "H1", "V1")
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201

    # 同一批在同一交接内再次退回 -> 409，details 指向首次退回号
    resp = do_return(client, "RT2", "H1", ["B1"])
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "BATCH_ALREADY_RETURNED"
    assert err["details"][0]["batch_no"] == "B1"
    assert err["details"][0]["return_no"] == "RT1"

    # 混合清单（B1 已退回 + B2 未退回）-> 整体拒绝，B2 不部分释放
    resp = do_return(client, "RT2", "H1", ["B2", "B1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_ALREADY_RETURNED"
    assert _occupancy(client, "B2") == "H1"
    assert _return_rows(client, "RT2") == (0, 0)

    # 全部退回后整单再退同样拒绝
    assert do_return(client, "RT3", "H1", ["B2"]).status_code == 201
    resp = do_return(client, "RT4", "H1", ["B1", "B2"])
    assert resp.status_code == 409
    assert {d["batch_no"] for d in resp.json()["error"]["details"]} == {"B1", "B2"}


def test_return_unknown_404(client):
    resp = client.get("/api/returns/NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RETURN_NOT_FOUND"


# ---------------------------------------------------------------- 查询与追溯


def test_return_queries_and_batch_whereabouts(client):
    release_batches(client, "B1", "B2", "B3")
    create_verification(client, "V1", ["B1", "B2", "B3"])
    confirm(client, "V1")
    handover(client, "H1", "V1")
    assert do_return(client, "RT1", "H1", ["B1"], reason="客户拒收").status_code == 201
    assert do_return(client, "RT2", "H1", ["B3"], reason="运输破损").status_code == 201

    # 退回列表（按交接过滤）
    all_returns = client.get("/api/returns").json()["returns"]
    assert [r["return_no"] for r in all_returns] == ["RT1", "RT2"]
    filtered = client.get("/api/returns?handover_no=H1").json()["returns"]
    assert [r["return_no"] for r in filtered] == ["RT1", "RT2"]
    assert filtered[0]["batches"] == [{"position": 1, "batch_no": "B1"}]
    assert filtered[0]["reason"] == "客户拒收"
    assert filtered[0]["handover_no"] == "H1"
    assert filtered[0]["verification_no"] == "V1"

    # 退回详情：冻结引用 + 逐批快照副本
    detail = client.get("/api/returns/RT2").json()
    assert detail["batch_count"] == 1
    assert detail["batches"][0]["batch_no"] == "B3"
    assert detail["batches"][0]["handover_position"] == 3
    assert detail["batches"][0]["adjudication"]["disposition"] == "RELEASE"
    assert detail["released"] == [{"batch_no": "B3", "handover_no": "H1"}]

    # 按批次查历次交接、退回及当前去向
    t1 = client.get("/api/batches/B1/handovers").json()
    assert t1["shipped"] is False and t1["current"] is None
    assert len(t1["handovers"]) == 1
    assert t1["handovers"][0]["returned"] is True
    assert t1["handovers"][0]["return"]["reason"] == "客户拒收"

    t2 = client.get("/api/batches/B2/handovers").json()
    assert t2["shipped"] is True
    assert t2["current"]["handover_no"] == "H1"
    assert t2["handovers"][0]["returned"] is False

    # 从未交接过的批次：历史为空、未发运
    release_batches(client, "B9", lot="RM-M")
    t9 = client.get("/api/batches/B9/handovers").json()
    assert t9 == {"batch_no": "B9", "shipped": False, "current": None, "handovers": []}

    # 未知批次 404
    resp = client.get("/api/batches/GHOST/handovers")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"


def test_batch_returned_then_reshipped_history_shows_both_handovers(client):
    """退回再发运后：批次历史含两笔交接，当前去向指向新交接。"""
    shipped_batch(client, "B1", "V1", "H1")
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201
    create_verification(client, "V2", ["B1"])
    d2 = confirm(client, "V2")
    handover(client, "H2", "V2", d2["revision"])

    trace = client.get("/api/batches/B1/handovers").json()
    assert [h["handover_no"] for h in trace["handovers"]] == ["H1", "H2"]
    assert trace["handovers"][0]["returned"] is True
    assert trace["handovers"][1]["returned"] is False
    assert trace["current"]["handover_no"] == "H2"
    # 单批当前归属端点同样指向新交接
    cur = client.get("/api/batches/B1/handover").json()
    assert cur["shipped"] is True
    assert cur["handover"]["handover_no"] == "H2"


# ---------------------------------------------------------------- 原子性与并发


def test_failed_return_leaves_no_rows_and_no_release(client):
    """各类拒绝路径：不写退回主/从记录，不释放任何占用。"""
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1", "B2"])
    confirm(client, "V1")
    handover(client, "H1", "V1")

    # 触发多种失败
    assert do_return(client, "RT1", "H1", ["B1", "GHOST"]).status_code == 404
    assert do_return(client, "RT1", "H1", ["B1"], reason=" ").status_code == 400
    assert do_return(client, "RT1", "NOPE", ["B1"]).status_code == 404

    assert _return_rows(client, "RT1") == (0, 0)
    assert _occupancy(client, "B1") == "H1"
    assert _occupancy(client, "B2") == "H1"
    # 修正后仍可正常退回
    assert do_return(client, "RT1", "H1", ["B1"]).status_code == 201
    assert _occupancy(client, "B1") is None
    assert _occupancy(client, "B2") == "H1"


def test_concurrent_returns_same_batch_exactly_one_succeeds(client, monkeypatch):
    """并发两笔退回同一批：写事务串行化，恰一笔成功，另一笔 409 且不释放。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")
    handover(client, "H1", "V1")

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

    def run(key, rno):
        try:
            payload = service.create_return(rno, "H1", "并发退回", ["B1"])
            results[key] = (201, payload["return_no"])
        except Exception as exc:  # APIError
            results[key] = (exc.status_code, exc.code)

    t1 = threading.Thread(target=run, args=("a", "RT-A"))
    t1.start()
    inside.wait(timeout=5)
    t2 = threading.Thread(target=run, args=("b", "RT-B"))
    t2.start()
    t2.join(timeout=0.5)
    assert t2.is_alive()  # 串行等待写锁
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    statuses = sorted(v[0] for v in results.values())
    assert statuses == [201, 409], results
    failed = [v for v in results.values() if v[0] == 409][0]
    assert failed[1] == "BATCH_ALREADY_RETURNED"

    # 恰一笔退回记录；B1 已释放
    returns = client.get("/api/returns").json()["returns"]
    assert len(returns) == 1
    assert _occupancy(client, "B1") is None


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_without_return_tables_upgrades_and_works(tmp_path):
    """无退回表的旧库启动后幂等建表，既有交接可登记退回并再次发运。"""
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
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE shipment_handover (handover_no TEXT PRIMARY KEY,
            verification_no TEXT NOT NULL, verification_revision INTEGER NOT NULL,
            confirmation_id INTEGER NOT NULL, confirmed_at TEXT NOT NULL,
            batch_count INTEGER NOT NULL, handed_over_at TEXT NOT NULL);
        CREATE TABLE shipment_handover_batch (handover_no TEXT NOT NULL,
            position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            confirmation_id INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]', snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER NOT NULL, snapshot_ship_status TEXT NOT NULL,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (handover_no, batch_no));
        CREATE TABLE shipment_handover_occupancy (batch_no TEXT PRIMARY KEY,
            handover_no TEXT NOT NULL, position INTEGER NOT NULL, handed_over_at TEXT NOT NULL);
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

        # 既有流程：登记 -> 核验 -> 确认 -> 交接 -> 退回 -> 再发运
        add_lot(c, "RM-M", "奶粉", ["milk"])
        add_batch(c, "B1", lots=["RM-M"], declared=["milk"])
        create_verification(c, "V1", ["B1"])
        confirm(c, "V1")
        handover(c, "H1", "V1")
        resp = do_return(c, "RT1", "H1", ["B1"], reason="旧库升级后退回")
        assert resp.status_code == 201, resp.text
        assert resp.json()["verification_revision"] == 2
        assert c.get("/api/batches/B1/handover").json()["shipped"] is False

        create_verification(c, "V2", ["B1"])
        d2 = confirm(c, "V2")
        handover(c, "H2", "V2", d2["revision"])
        trace = c.get("/api/batches/B1/handovers").json()
        assert [h["handover_no"] for h in trace["handovers"]] == ["H1", "H2"]
        assert trace["current"]["handover_no"] == "H2"

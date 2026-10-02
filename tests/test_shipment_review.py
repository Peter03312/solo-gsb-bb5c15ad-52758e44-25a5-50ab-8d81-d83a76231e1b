"""已发运批次发运后质量复核测试。

覆盖：
- 查询（GET /api/handovers/{no}/reviews）：逐批并列交接冻结快照与当前版本/结论/作废状态；
  无变化 NO_REVIEW_REQUIRED；裁决版本变化（即使结论仍 RELEASE）/出货资格版本变化/批次作废
  均形成复核事项 PENDING_REVIEW；冻结快照本身不改；
- 处置（POST .../reviews/{batch_no}/decision）：凭两类当前版本 + 作废状态的所见状态提交
  RECALL_REQUIRED/NO_RECALL 与非空原因；写入只追加历史后显示已复核；
  后续版本变化或新作废再次待复核并可再次处置，历次处置保留；
- 拒绝：未知交接 404、批次不属于该交接 404、非法处置结论/空原因 400、
  过期所见状态 409（REVIEW_STATE_STALE）、同一状态重复处置 409（REVIEW_ALREADY_RECORDED）、
  无变化状态处置 409（REVIEW_NOT_REQUIRED）；所有拒绝均不写入；
- 交接快照与原核验/交接流程保持不变；多批混合状态汇总与保序；旧库启动幂等建新表。
"""
from __future__ import annotations

import sqlite3
import threading

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


def lock(client, lot_no, reason="待检风险", locked=True):
    path = "lock" if locked else "unlock"
    resp = client.post(
        f"/api/material-lots/{lot_no}/{path}", json={"reason": reason}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_shipped_handover(client, *batch_nos, handover_no="H1", verification_no="V1",
                          line_id="L1", order=None):
    """登记核验单 -> 确认 -> 交接，返回交接响应。"""
    doc = client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": order or list(batch_nos)},
    )
    assert doc.status_code == 201, doc.text
    resp = client.post(f"/api/verifications/{verification_no}/confirm")
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]
    resp = client.post(
        "/api/handovers",
        json={
            "handover_no": handover_no,
            "verification_no": verification_no,
            "expected_revision": revision,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def release_line(client, *batch_nos, lot="RM-M", line_id="L1"):
    add_lot(client, lot, "奶粉", ["milk"])
    for bn in batch_nos:
        add_batch(client, bn, line_id=line_id, lots=[lot], declared=["milk"])


def board(client, handover_no="H1"):
    resp = client.get(f"/api/handovers/{handover_no}/reviews")
    assert resp.status_code == 200, resp.text
    return resp.json()


def board_item(client, batch_no, handover_no="H1"):
    return next(
        b for b in board(client, handover_no)["batches"] if b["batch_no"] == batch_no
    )


def decide(client, batch_no, payload, handover_no="H1"):
    return client.post(
        f"/api/handovers/{handover_no}/reviews/{batch_no}/decision", json=payload
    )


def seen_of(item):
    return {
        "seen_adjudication_version": item["seen_state"]["adjudication_version"],
        "seen_shipment_version": item["seen_state"]["shipment_version"],
        "seen_batch_voided": item["seen_state"]["batch_voided"],
    }


# ---------------------------------------------------------------- 查询：无变化


def test_fresh_handover_batches_need_no_review(client):
    release_line(client, "B1", "B2")
    make_shipped_handover(client, "B1", "B2", order=["B2", "B1"])

    doc = board(client)
    assert doc["summary"] == {
        "pending_review": 0,
        "reviewed": 0,
        "no_review_required": 2,
        "pending_batches": [],
        "reviewed_batches": [],
    }
    assert [b["batch_no"] for b in doc["batches"]] == ["B2", "B1"]  # 交接清单保序
    for b in doc["batches"]:
        assert b["review_status"] == "NO_REVIEW_REQUIRED"
        assert b["needs_review"] is False
        assert b["adjudication_changed"] is False
        assert b["shipment_eligibility_changed"] is False
        assert b["voided"] is False
        assert b["change_reasons"] == []
        assert b["shipment_change_reasons"] == []
        assert b["reviews"] == []
        assert b["latest_review"] is None
        # 交接冻结快照
        assert b["handover_snapshot"]["adjudication"]["version"] == 1
        assert b["handover_snapshot"]["adjudication"]["disposition"] == "RELEASE"
        assert b["handover_snapshot"]["shipment_eligibility"]["version"] == 1
        assert b["handover_snapshot"]["shipment_eligibility"]["status"] == "SHIPPABLE"
        # 当前版本并列
        assert b["current"]["adjudication"]["version"] == 1
        assert b["current"]["shipment_eligibility"]["version"] == 1
        assert b["seen_state"] == {
            "adjudication_version": 1,
            "shipment_version": 1,
            "batch_voided": False,
        }


def test_get_review_board_unknown_handover_404(client):
    resp = client.get("/api/handovers/NOPE/reviews")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HANDOVER_NOT_FOUND"


# ---------------------------------------------------------------- 出货资格版本变化


def test_lot_lock_after_handover_creates_review_item(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")

    lock(client, "RM-M", "供应商通报风险待复检")

    item = board_item(client, "B1")
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["needs_review"] is True
    assert item["adjudication_changed"] is False          # 裁决版本未变
    assert item["shipment_eligibility_changed"] is True   # 资格版本递增
    assert item["current"]["shipment_eligibility"]["version"] == 2
    assert item["current"]["shipment_eligibility"]["status"] == "BLOCKED"
    assert item["seen_state"] == {
        "adjudication_version": 1,
        "shipment_version": 2,
        "batch_voided": False,
    }
    # 变化原因逐条给出（即使结论变化或不变化）
    assert len(item["shipment_change_reasons"]) == 1
    assert item["shipment_change_reasons"][0]["trigger"] == "LOT_LOCK"
    assert item["change_reasons"] == []


def test_reviewed_then_new_version_pending_again_keeps_history(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")

    lock(client, "RM-M", "风险待检")
    item = board_item(client, "B1")
    payload = {
        **seen_of(item),
        "disposition": "RECALL_REQUIRED",
        "reason": "直接用料待检锁定，已发运产品需召回",
    }
    resp = decide(client, "B1", payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["reviewed"] is True
    assert resp.json()["batch"]["review_status"] == "REVIEWED"

    item = board_item(client, "B1")
    assert item["review_status"] == "REVIEWED"
    assert item["latest_review"]["disposition"] == "RECALL_REQUIRED"
    assert item["latest_review"]["disposition_label"] == "需召回"
    assert len(item["reviews"]) == 1

    # 解除锁定：资格版本 v3 -> 再次待复核；历史处置保留
    lock(client, "RM-M", "复检合格", locked=False)
    item = board_item(client, "B1")
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["seen_state"]["shipment_version"] == 3
    assert item["current"]["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert len(item["reviews"]) == 1  # 历史保留

    payload = {**seen_of(item), "disposition": "NO_RECALL", "reason": "原料复检合格，无需召回"}
    resp = decide(client, "B1", payload)
    assert resp.status_code == 201, resp.text

    item = board_item(client, "B1")
    assert item["review_status"] == "REVIEWED"
    assert item["latest_review"]["disposition_label"] == "无需召回"
    assert [h["disposition"] for h in item["reviews"]] == ["RECALL_REQUIRED", "NO_RECALL"]
    assert [h["seen_state"]["shipment_version"] for h in item["reviews"]] == [2, 3]


# ---------------------------------------------------------------- 裁决版本变化（结论仍 RELEASE）


def test_adjudication_version_change_even_if_release_is_review_item(client):
    """清洁更正使 B1 追加裁决版本但结论仍为 RELEASE：仍构成复核事项。"""
    add_lot(client, "RM-M", "奶粉", ["milk"])
    # B1 声明 milk；额外清洁 milk 不影响结论
    add_batch(
        client, "B1", lots=["RM-M"], declared=["milk"], cleaned=["milk"]
    )
    make_shipped_handover(client, "B1")

    # 更正清洁集合：加入一个本就不存在残留的过敏原（如 gluten 不在链上），
    # 结论保持 RELEASE，但追加 CORRECT_BATCH 裁决版本 v2
    resp = client.put(
        "/api/batches/B1/cleaning", json={"cleaned_allergens": ["milk", "gluten"]}
    )
    assert resp.status_code == 200, resp.text
    batch = resp.json()["corrected"]
    assert batch["adjudication"]["version"] == 2
    assert batch["adjudication"]["disposition"] == "RELEASE"

    item = board_item(client, "B1")
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["adjudication_changed"] is True
    assert item["shipment_eligibility_changed"] is False
    assert item["current"]["adjudication"]["version"] == 2
    assert item["current"]["adjudication"]["disposition"] == "RELEASE"
    assert item["change_reasons"][0]["trigger"] == "CORRECT_BATCH"
    assert item["seen_state"] == {
        "adjudication_version": 2,
        "shipment_version": 1,
        "batch_voided": False,
    }

    payload = {**seen_of(item), "disposition": "NO_RECALL", "reason": "新版本结论仍为放行，评估后无需召回"}
    assert decide(client, "B1", payload).status_code == 201
    assert board_item(client, "B1")["review_status"] == "REVIEWED"


def test_adjudication_change_to_hold_after_handover_is_review_item(client):
    """交接后更正使已发运批次结论变 HOLD：待复核且变化原因带 HOLD 结论。"""
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    make_shipped_handover(client, "B1")

    # 原料批号追加未声明过敏原 peanut -> B1 重算为 HOLD
    resp = client.put("/api/material-lots/RM-M", json={"allergens": ["milk", "peanut"]})
    assert resp.status_code == 200, resp.text

    item = board_item(client, "B1")
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["current"]["adjudication"]["version"] == 2
    assert item["current"]["adjudication"]["disposition"] == "HOLD"
    assert "peanut" in item["current"]["adjudication"]["undeclared_allergens"]

    payload = {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": "已发运批次含未声明花生，必须召回"}
    assert decide(client, "B1", payload).status_code == 201


# ---------------------------------------------------------------- 批次作废


def test_voided_batch_after_handover_is_review_item(client):
    # B1/B2 分属不同产线：作废 B1 不会重算 B2，便于隔离断言
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", line_id="L1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", line_id="L2", lots=["RM-M"], declared=["milk"])
    make_shipped_handover(client, "B1", "B2")

    resp = client.post("/api/batches/B1/void", json={"reason": "误登记，实际未投产"})
    assert resp.status_code == 200, resp.text

    item = board_item(client, "B1")
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["voided"] is True
    assert item["status"] == "VOIDED"
    assert item["voided_reason"] == "误登记，实际未投产"
    # 作废本身形成变化原因（不追加版本号）
    assert any(r["code"] == "BATCH_VOIDED" for r in item["change_reasons"])
    assert item["seen_state"]["batch_voided"] is True
    # 作废后裁决/资格当前记录冻结保留在其最后版本
    assert item["current"]["adjudication"]["version"] == 1
    assert item["current"]["shipment_eligibility"]["version"] == 1

    payload = {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": "批次已作废却发运，须召回核查"}
    assert decide(client, "B1", payload).status_code == 201
    assert board_item(client, "B1")["review_status"] == "REVIEWED"

    # B2 在另一条产线、未作废且无变化
    assert board_item(client, "B2")["review_status"] == "NO_REVIEW_REQUIRED"


# ---------------------------------------------------------------- 多批混合汇总


def test_mixed_batches_summary_and_order(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_lot(client, "RM-C", "可可块", [])
    # B1/B3 在 L1（B1 用奶、B3 用无过敏原可可）；B2 单独在 L2 以便作废不重算 L1
    add_batch(client, "B1", line_id="L1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B3", line_id="L1", lots=["RM-C"], declared=[], cleaned=["milk"])
    add_batch(client, "B2", line_id="L2", lots=["RM-M"], declared=["milk"])
    make_shipped_handover(client, "B1", "B2", "B3", order=["B3", "B1", "B2"])

    # B1 资格变化（直接用料待检锁定）；B2 作废；B3 用料与 L1 链均不受影响
    lock(client, "RM-M", "风险")
    client.post("/api/batches/B2/void", json={"reason": "误登记"})

    doc = board(client)
    assert [b["batch_no"] for b in doc["batches"]] == ["B3", "B1", "B2"]
    statuses = {b["batch_no"]: b["review_status"] for b in doc["batches"]}
    assert statuses == {
        "B1": "PENDING_REVIEW",
        "B2": "PENDING_REVIEW",
        "B3": "NO_REVIEW_REQUIRED",
    }
    assert doc["summary"]["pending_review"] == 2
    assert doc["summary"]["no_review_required"] == 1
    assert sorted(doc["summary"]["pending_batches"]) == ["B1", "B2"]

    # 只处置 B1：B2 仍待复核
    item_b1 = next(b for b in doc["batches"] if b["batch_no"] == "B1")
    assert decide(client, "B1", {**seen_of(item_b1), "disposition": "NO_RECALL", "reason": "ok"}).status_code == 201
    doc = board(client)
    assert doc["summary"]["pending_review"] == 1
    assert doc["summary"]["reviewed"] == 1
    assert doc["summary"]["pending_batches"] == ["B2"]
    assert doc["summary"]["reviewed_batches"] == ["B1"]


# ---------------------------------------------------------------- 拒绝路径


def test_decide_rejects_unknown_handover_and_foreign_batch(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")

    body = {
        "seen_adjudication_version": 1,
        "seen_shipment_version": 1,
        "seen_batch_voided": False,
        "disposition": "NO_RECALL",
        "reason": "x",
    }
    resp = client.post("/api/handovers/NOPE/reviews/B1/decision", json=body)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HANDOVER_NOT_FOUND"

    # 已登记但不属于该交接的批次
    add_batch(client, "B9", lots=["RM-M"], declared=["milk"])
    resp = decide(client, "B9", body)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_IN_HANDOVER"

    # 无任何处置写入
    assert board_item(client, "B1")["reviews"] == []


def test_decide_rejects_blank_reason_and_bad_disposition(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")
    lock(client, "RM-M", "风险")
    item = board_item(client, "B1")

    for reason in ("", "   ", "\t\n"):
        resp = decide(
            client,
            "B1",
            {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": reason},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"

    resp = decide(
        client, "B1", {**seen_of(item), "disposition": "HOLD", "reason": "x"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    assert board_item(client, "B1")["reviews"] == []


def test_decide_rejects_no_change_state(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")
    # 交接后无任何变化即处置 -> REVIEW_NOT_REQUIRED
    resp = decide(
        client,
        "B1",
        {
            "seen_adjudication_version": 1,
            "seen_shipment_version": 1,
            "seen_batch_voided": False,
            "disposition": "NO_RECALL",
            "reason": "无变化也提交",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_NOT_REQUIRED"
    assert board_item(client, "B1")["reviews"] == []


def test_decide_rejects_stale_seen_state(client):
    release_line(client, "B1", "B2")
    make_shipped_handover(client, "B1", "B2")
    lock(client, "RM-M", "风险")
    item = board_item(client, "B1")
    assert item["seen_state"]["shipment_version"] == 2

    # 所见资格版本停留在 v1（过期）
    resp = decide(
        client,
        "B1",
        {
            "seen_adjudication_version": 1,
            "seen_shipment_version": 1,
            "seen_batch_voided": False,
            "disposition": "RECALL_REQUIRED",
            "reason": "基于旧版本",
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "REVIEW_STATE_STALE"
    fields = {f["field"] for f in err["details"][0]["stale_fields"]}
    assert fields == {"shipment_version"}

    # 作废状态过期（实际已作废却提交 false）
    client.post("/api/batches/B2/void", json={"reason": "误登记"})
    item2 = board_item(client, "B2")
    resp = decide(
        client,
        "B2",
        {
            "seen_adjudication_version": item2["seen_state"]["adjudication_version"],
            "seen_shipment_version": item2["seen_state"]["shipment_version"],
            "seen_batch_voided": False,
            "disposition": "RECALL_REQUIRED",
            "reason": "基于旧作废状态",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_STATE_STALE"
    assert board_item(client, "B1")["reviews"] == []
    assert board_item(client, "B2")["reviews"] == []


def test_decide_same_state_not_repeatable(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")
    lock(client, "RM-M", "风险")
    item = board_item(client, "B1")
    body = {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": "首次处置"}
    assert decide(client, "B1", body).status_code == 201

    # 同一所见状态再次处置（即使结论/原因不同）-> 409，历史不增加
    resp = decide(
        client, "B1", {**seen_of(item), "disposition": "NO_RECALL", "reason": "重复处置"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_ALREADY_RECORDED"
    item = board_item(client, "B1")
    assert len(item["reviews"]) == 1
    assert item["reviews"][0]["disposition"] == "RECALL_REQUIRED"
    assert item["latest_review"]["review_id"] == item["reviews"][0]["review_id"]


def test_concurrent_same_state_only_one_wins(client):
    """并发对同一所见状态提交两次处置：恰有一条成功，另一条 REVIEW_ALREADY_RECORDED。"""
    release_line(client, "B1")
    make_shipped_handover(client, "B1")
    lock(client, "RM-M", "风险")
    item = board_item(client, "B1")
    body = {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": "并发处置"}

    results: list[int] = []
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait()
        results.append(decide(client, "B1", body).status_code)

    t1 = threading.Thread(target=submit)
    t2 = threading.Thread(target=submit)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [201, 409]
    assert len(board_item(client, "B1")["reviews"]) == 1


# ---------------------------------------------------------------- 原流程不变


def test_handover_snapshot_and_original_flows_unchanged(client):
    """复核功能不改写交接冻结快照，也不影响核验/交接原流程。"""
    release_line(client, "B1")
    h = make_shipped_handover(client, "B1")

    lock(client, "RM-M", "风险")
    item = board_item(client, "B1")
    decide(
        client,
        "B1",
        {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": "召回"},
    )

    # 交接详情中的冻结快照保持交接时版本/结论
    detail = client.get("/api/handovers/H1").json()
    frozen = detail["batches"][0]
    assert frozen["adjudication"]["version"] == 1
    assert frozen["adjudication"]["disposition"] == "RELEASE"
    assert frozen["shipment_eligibility"]["version"] == 1
    assert frozen["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert detail["handed_over_at"] == h["handed_over_at"]

    # 核验单仍因锁定转待复核（原流程），交接不改变其状态
    vdoc = client.get("/api/verifications/V1").json()
    assert vdoc["status"] == "PENDING_REVIEW"

    # 批次仍被占用、归属仍指向原交接
    trace = client.get("/api/batches/B1/handover").json()
    assert trace["shipped"] is True
    assert trace["handover"]["handover_no"] == "H1"

    # 复核历史为只追加：两次状态两个处置记录
    assert len(board_item(client, "B1")["reviews"]) == 1


def test_decide_returns_recorded_item(client):
    release_line(client, "B1")
    make_shipped_handover(client, "B1")
    lock(client, "RM-M", "风险")
    item = board_item(client, "B1")
    resp = decide(
        client,
        "B1",
        {**seen_of(item), "disposition": "NO_RECALL", "reason": "评估无需召回"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["handover_no"] == "H1"
    assert body["reviewed"] is True
    assert isinstance(body["review_id"], int)
    assert body["batch"]["batch_no"] == "B1"
    assert body["batch"]["review_status"] == "REVIEWED"
    assert body["batch"]["latest_review"]["reason"] == "评估无需召回"


# ---------------------------------------------------------------- 旧库升级


def test_review_table_available_on_existing_db(tmp_path):
    """在已有交接数据的旧库上以新代码启动：复核表幂等建立，既有交接可查询复核。"""
    db_path = str(tmp_path / "old.db")
    app = create_app(db_path=db_path, seed_demo=False)
    with TestClient(app) as c:
        add_lot(c, "RM-M", "奶粉", ["milk"])
        add_batch(c, "B1", lots=["RM-M"], declared=["milk"])
        make_shipped_handover(c, "B1")

    # 模拟本轮功能上线前的旧库：移除新表与其索引（其余表/数据保留）
    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX IF EXISTS idx_ship_review_state_once")
    raw.execute("DROP INDEX IF EXISTS idx_ship_review_handover_batch")
    raw.execute("DROP TABLE IF EXISTS shipment_review")
    raw.commit()
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    raw.close()
    assert "shipment_handover" in tables
    assert "shipment_review" not in tables

    # 新代码再次启动：自动建表，既有交接可查、可处置
    new = create_app(db_path=db_path, seed_demo=False)
    with TestClient(new) as c:
        doc = board(c)
        assert doc["batches"][0]["review_status"] == "NO_REVIEW_REQUIRED"
        lock(c, "RM-M", "风险")
        item = board_item(c, "B1")
        assert item["review_status"] == "PENDING_REVIEW"
        assert decide(
            c,
            "B1",
            {**seen_of(item), "disposition": "RECALL_REQUIRED", "reason": "升级后复核"},
        ).status_code == 201

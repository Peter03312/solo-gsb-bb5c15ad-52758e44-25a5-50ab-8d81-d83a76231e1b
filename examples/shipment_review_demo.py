#!/usr/bin/env python3
"""食品换线放行服务 —— 已发运批次发运后质量复核端到端调用示例（仅标准库）。

背景：批次完成装车交接（整单发运）后，可能发生后续更正（裁决版本）、原料批号待检
锁定（出货资格版本）或批次作废。质检员需按交接号逐批复核风险，对每个“所见状态”
记录“需召回/无需召回”与非空原因；处置写入只追加历史后显示已复核，后续版本变化或
新作废再次待复核；同一状态不得重复处置。

演示流程（批次/批号/单号均带时间戳，可重复运行，无需重置库）：
  1. 各自独立产线登记 3 个全部 RELEASE/SHIPPABLE 的批次，创建核验单→确认→整单交接
  2. 交接后无变化：复核事项为 NO_REVIEW_REQUIRED；此时提交处置被 409 REVIEW_NOT_REQUIRED
  3. 原料批号待检锁定（出货资格版本递增）-> 批次 PENDING_REVIEW；提交“需召回” -> REVIEWED
  4. 批次作废 -> PENDING_REVIEW（作废单独构成变化原因）；提交“无需召回” -> REVIEWED
  5. 清洁更正追加裁决版本但结论仍 RELEASE -> 仍 PENDING_REVIEW；提交“无需召回”
  6. 拒绝：未知交接 404、批次不属于该交接 404、过期所见状态 409、同状态重复处置 409、
     空原因 400、非法处置结论 400（均不写入）
  7. 解除锁定：出货资格新版本 -> 已复核批次再次 PENDING_REVIEW，可再处置；
     只追加历史保留两次处置；交接冻结快照始终不变

用法：
    python examples/shipment_review_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/shipment_review_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def must(status: int, expected: int, body: dict, label: str) -> None:
    assert status == expected, f"{label} 期望 {expected}，实际 {status}: {body}"


def board_item(board: dict, batch_no: str) -> dict:
    return next(b for b in board["batches"] if b["batch_no"] == batch_no)


def decide(board: dict, batch_no: str, disposition: str, reason: str) -> tuple[int, dict]:
    """凭复核事项中的 seen_state 提交处置。"""
    item = board_item(board, batch_no)
    seen = item["seen_state"]
    return call(
        "POST",
        f"/api/handovers/{board['handover_no']}/reviews/{batch_no}/decision",
        {
            "seen_adjudication_version": seen["adjudication_version"],
            "seen_shipment_version": seen["shipment_version"],
            "seen_batch_voided": seen["batch_voided"],
            "disposition": disposition,
            "reason": reason,
        },
    )


def print_status_line(board: dict) -> None:
    for b in board["batches"]:
        cur_a = b["current"]["adjudication"]
        cur_s = b["current"]["shipment_eligibility"]
        print(
            f"  position {b['position']} {b['batch_no']}: {b['review_status']} "
            f"(voided={b['voided']}, 裁决 v{cur_a['version']}/{cur_a['disposition']}, "
            f"资格 v{cur_s['version']}/{cur_s['status']})"
        )


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    lot_a, lot_b, lot_c = f"RV-A-{suffix}", f"RV-B-{suffix}", f"RV-C-{suffix}"
    b1, b2, b3 = f"RV001-{suffix}", f"RV002-{suffix}", f"RV003-{suffix}"
    v1, h1 = f"RVV-{suffix}", f"RVH-{suffix}"

    section("1. 准备：3 个独立产线上的 RELEASE/SHIPPABLE 批次 -> 核验 -> 确认 -> 整单交接")
    for lot_no, material, allergens in (
        (lot_a, "复核示例奶粉", ["milk"]),
        (lot_b, "复核示例奶粉2", ["milk"]),
        (lot_c, "复核示例可可块", []),
    ):
        st, body = call("POST", "/api/material-lots", {
            "lot_no": lot_no, "material": material, "allergens": allergens,
        })
        must(st, 201, body, f"登记原料批号 {lot_no}")
    # 各自独立产线：批次间无残留传递，后续变化可逐批隔离
    st, body = call("POST", "/api/batches", {
        "batch_no": b1, "line_id": f"LRV1-{suffix}", "material_lots": [lot_a],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    must(st, 201, body, "登记批次 1")
    st, body = call("POST", "/api/batches", {
        "batch_no": b2, "line_id": f"LRV2-{suffix}", "material_lots": [lot_b],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    must(st, 201, body, "登记批次 2")
    st, body = call("POST", "/api/batches", {
        "batch_no": b3, "line_id": f"LRV3-{suffix}", "material_lots": [lot_c],
        "declared_allergens": [], "cleaned_allergens": [],
    })
    must(st, 201, body, "登记批次 3")

    st, body = call("POST", "/api/verifications", {
        "verification_no": v1, "batch_nos": [b2, b1, b3],
    })
    must(st, 201, body, "创建核验单")
    st, doc = call("POST", f"/api/verifications/{v1}/confirm")
    must(st, 200, doc, "确认核验单")
    st, body = call("POST", "/api/handovers", {
        "handover_no": h1, "verification_no": v1, "expected_revision": doc["revision"],
    })
    must(st, 201, body, "装车交接")
    print(f"  已交接 {h1}（核验单 {v1}，冻结修订号 v{body['verification_revision']}，"
          f"{body['batch_count']} 批，顺序 [b2, b1, b3]）")

    section("2. 交接后查询复核事项：相较快照无变化 -> 全部 NO_REVIEW_REQUIRED")
    st, board = call("GET", f"/api/handovers/{h1}/reviews")
    must(st, 200, board, "查询复核事项")
    print(f"  summary: {board['summary']}")
    print_status_line(board)
    assert all(b["review_status"] == "NO_REVIEW_REQUIRED" for b in board["batches"])

    # 无变化状态提交处置 -> REVIEW_NOT_REQUIRED
    st, body = decide(board, b1, "NO_RECALL", "无变化也尝试处置")
    print(f"[{st}] 对无变化批次处置 -> {body['error']['code']}")
    assert st == 409 and body["error"]["code"] == "REVIEW_NOT_REQUIRED"

    section("3. 原料批号待检锁定：出货资格版本递增（裁决未变）-> PENDING_REVIEW")
    st, body = call("POST", f"/api/material-lots/{lot_a}/lock", {
        "reason": "供应商通报真菌毒素风险，待复检",
    })
    must(st, 200, body, "锁定批号 A")
    st, board = call("GET", f"/api/handovers/{h1}/reviews")
    must(st, 200, board, "查询复核事项")
    print_status_line(board)
    item = board_item(board, b1)
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["shipment_eligibility_changed"] is True
    assert item["adjudication_changed"] is False
    print(f"  变化原因: {[r['message'] for r in item['shipment_change_reasons']]}")
    print(f"  提交处置时须回传的 seen_state: {item['seen_state']}")

    st, body = decide(board, b1, "RECALL_REQUIRED",
                      "直接使用的原料批号待检锁定，已发运产品存在安全风险，需召回")
    must(st, 201, body, "处置：需召回")
    print(f"[{st}] {b1} 处置成功 review_id={body['review_id']} -> {body['batch']['review_status']}")

    section("4. 批次作废 -> PENDING_REVIEW（作废单独构成复核事项）")
    st, body = call("POST", f"/api/batches/{b2}/void", {"reason": "计划误登记，该批实际未投产"})
    must(st, 200, body, "作废批次 2")
    st, board = call("GET", f"/api/handovers/{h1}/reviews")
    item = board_item(board, b2)
    assert item["review_status"] == "PENDING_REVIEW" and item["voided"] is True
    print(f"  {b2} 作废原因: {item['voided_reason']}")
    print(f"  变化原因: {[r['code'] for r in item['change_reasons']]}")
    st, body = decide(board, b2, "NO_RECALL",
                      "核查装车记录该批已在月台拦截、实际未发出，无需召回")
    must(st, 201, body, "处置：无需召回")
    print(f"[{st}] {b2} 处置成功 -> {body['batch']['review_status']}")

    section("5. 清洁更正追加裁决版本（结论仍 RELEASE）-> 仍 PENDING_REVIEW")
    st, body = call("PUT", f"/api/batches/{b3}/cleaning", {"cleaned_allergens": ["milk"]})
    must(st, 200, body, "清洁更正批次 3")
    assert body["corrected"]["adjudication"]["version"] == 2
    assert body["corrected"]["adjudication"]["disposition"] == "RELEASE"
    st, board = call("GET", f"/api/handovers/{h1}/reviews")
    item = board_item(board, b3)
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["adjudication_changed"] is True
    print(f"  {b3} 当前裁决 v{item['current']['adjudication']['version']}，"
          f"结论仍 {item['current']['adjudication']['disposition']}（即使放行也须复核）")
    st, body = decide(board, b3, "NO_RECALL",
                      "新版本裁决仍为 RELEASE，实际过敏原与声明无变化，无需召回")
    must(st, 201, body, "处置：无需召回")
    print(f"[{st}] {b3} 处置成功 -> {body['batch']['review_status']}")

    section("6. 拒绝场景（均不写入）")
    st, board = call("GET", f"/api/handovers/{h1}/reviews")

    # 未知交接
    st, body = call("GET", "/api/handovers/NO-SUCH-HANDOVER/reviews")
    print(f"[{st}] 查询未知交接 -> {body['error']['code']}")
    assert st == 404 and body["error"]["code"] == "HANDOVER_NOT_FOUND"

    # 批次不属于该交接
    st, body = call("POST", f"/api/handovers/{h1}/reviews/NO-SUCH-BATCH/decision", {
        "seen_adjudication_version": 1, "seen_shipment_version": 1,
        "seen_batch_voided": False, "disposition": "NO_RECALL", "reason": "x",
    })
    print(f"[{st}] 处置非本交接批次 -> {body['error']['code']}")
    assert st == 404 and body["error"]["code"] == "BATCH_NOT_IN_HANDOVER"

    # 过期所见状态（b1 已处置于资格 v2；手工提交过期的 v1）
    st, body = call("POST", f"/api/handovers/{h1}/reviews/{b1}/decision", {
        "seen_adjudication_version": 1, "seen_shipment_version": 1,
        "seen_batch_voided": False, "disposition": "RECALL_REQUIRED", "reason": "基于旧版本",
    })
    print(f"[{st}] 过期所见状态 -> {body['error']['code']} "
          f"(不符字段: {[f['field'] for f in body['error']['details'][0]['stale_fields']]})")
    assert st == 409 and body["error"]["code"] == "REVIEW_STATE_STALE"

    # 同一状态重复处置（b3 当前状态已处置）
    item = board_item(board, b3)
    seen = item["seen_state"]
    st, body = call("POST", f"/api/handovers/{h1}/reviews/{b3}/decision", {
        "seen_adjudication_version": seen["adjudication_version"],
        "seen_shipment_version": seen["shipment_version"],
        "seen_batch_voided": seen["batch_voided"],
        "disposition": "RECALL_REQUIRED", "reason": "对同一状态再处置一次",
    })
    print(f"[{st}] 同一状态重复处置 -> {body['error']['code']}")
    assert st == 409 and body["error"]["code"] == "REVIEW_ALREADY_RECORDED"

    # 空原因 / 非法处置结论
    st, body = call("POST", f"/api/handovers/{h1}/reviews/{b3}/decision", {
        "seen_adjudication_version": seen["adjudication_version"],
        "seen_shipment_version": seen["shipment_version"],
        "seen_batch_voided": seen["batch_voided"],
        "disposition": "NO_RECALL", "reason": "   ",
    })
    print(f"[{st}] 空白原因 -> {body['error']['code']}")
    assert st == 400 and body["error"]["code"] == "INVALID_PAYLOAD"
    st, body = call("POST", f"/api/handovers/{h1}/reviews/{b3}/decision", {
        "seen_adjudication_version": seen["adjudication_version"],
        "seen_shipment_version": seen["shipment_version"],
        "seen_batch_voided": seen["batch_voided"],
        "disposition": "MAYBE", "reason": "非法结论",
    })
    print(f"[{st}] 非法处置结论 -> {body['error']['code']}")
    assert st == 400 and body["error"]["code"] == "INVALID_PAYLOAD"

    section("7. 解除锁定：新出货资格版本 -> 已复核批次再次待复核，历史只追加")
    st, body = call("POST", f"/api/material-lots/{lot_a}/unlock", {"reason": "复检合格，解除待检"})
    must(st, 200, body, "解除批号 A 锁定")
    st, board = call("GET", f"/api/handovers/{h1}/reviews")
    item = board_item(board, b1)
    print(f"  {b1}: {item['review_status']}，seen_state={item['seen_state']}，"
          f"历史处置 {len(item['reviews'])} 条（保留）")
    assert item["review_status"] == "PENDING_REVIEW"
    assert item["seen_state"]["shipment_version"] == 3
    assert len(item["reviews"]) == 1

    st, body = decide(board, b1, "NO_RECALL", "原料复检合格解除锁定，评估无需召回")
    must(st, 201, body, "对新状态再次处置")
    st, board = call("GET", f"/api/handovers/{h1}/reviews")
    item = board_item(board, b1)
    print(f"  {b1}: {item['review_status']}")
    for h in item["reviews"]:
        print(f"    review_id={h['review_id']} 所见资格v{h['seen_state']['shipment_version']} "
              f"作废={h['seen_state']['batch_voided']} -> {h['disposition_label']}（{h['reason']}）")
    assert [h["disposition"] for h in item["reviews"]] == ["RECALL_REQUIRED", "NO_RECALL"]

    section("8. 最终汇总与交接冻结快照不变")
    print(f"  summary: {board['summary']}")
    print_status_line(board)
    st, ho = call("GET", f"/api/handovers/{h1}")
    must(st, 200, ho, "交接详情")
    for frozen in ho["batches"]:
        print(f"  冻结快照 {frozen['batch_no']}: 裁决 v{frozen['adjudication']['version']}/"
              f"{frozen['adjudication']['disposition']}，"
              f"资格 v{frozen['shipment_eligibility']['version']}/"
              f"{frozen['shipment_eligibility']['status']}")
    assert all(f["adjudication"]["version"] == 1 for f in ho["batches"])
    assert all(f["shipment_eligibility"]["version"] == 1 for f in ho["batches"])
    print("  交接冻结快照始终保持交接时版本（复核处置不改写交接记录与原核验流程）")

    print("\n全部发运后质量复核流程演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

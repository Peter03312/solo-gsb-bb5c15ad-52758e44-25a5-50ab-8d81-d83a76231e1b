#!/usr/bin/env python3
"""食品换线放行服务 —— 交接退回（已装车交接批次退货登记）端到端调用示例（仅标准库）。

演示流程（独立产线 LRT，单号带时间戳，可重复运行）：
  1. 登记原料批号与 3 个全部 RELEASE/SHIPPABLE 的批次 RT001/RT002/RT003
  2. 创建核验单 [RT001, RT002, RT003] 并确认，凭读取时修订号登记整单交接 HO
  3. 部分退回：凭唯一退回号 + 原交接号 + 非空原因 + 交接内批次清单登记退回
       -> 退回记录冻结原交接单号/修订号/确认引用与逐批交接快照副本
       -> 退回批次释放发运占用；未退回批次仍占用发运名额
  4. 不可变核对：原交接详情仍显示当时完整清单（逐批标注退回），
     交接冻结快照与发运后复核历史不改写
  5. 拒绝场景（整体拒绝、不留下部分释放）：
       重复退回号(409)、未知交接(404)、空原因(400)、清单内重复批次(400)、
       非本交接批次(404)、已退回批次(409)
  6. 退回批次再次发运：纳入新核验单完成当前版本确认 -> 新交接；
     未退回批次仍被原交接占用，含它的新交接整单拒绝
  7. 追溯：按批次查历次交接、退回及当前去向；退回列表/详情查询

用法：
    python examples/shipment_return_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/shipment_return_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LRT"


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


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> None:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        ship = body["shipment_eligibility"]
        print(f"  已登记批次 {batch_no} => 裁决 {adj['disposition']}（v{adj['version']}），"
              f"出货资格 {ship['status']}（v{ship['version']}）")
    elif status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    v1 = f"RTV-{suffix}"
    v2 = f"RTV2-{suffix}"
    v3 = f"RTV3-{suffix}"
    h1 = f"RTH-{suffix}"
    h2 = f"RTH2-{suffix}"
    rt1 = f"RT-{suffix}"
    rt2 = f"RT2-{suffix}"

    section("1. 准备数据：3 个全部 RELEASE/SHIPPABLE 的批次 RT001/RT002/RT003")
    ensure_lot("RT-RM-MILK", "退回应例奶粉", ["milk"])
    for bn in ("RT001", "RT002", "RT003"):
        ensure_batch(bn, {
            "batch_no": bn, "line_id": LINE,
            "material_lots": ["RT-RM-MILK"],
            "declared_allergens": ["milk"], "cleaned_allergens": [],
        })

    section(f"2. 创建并确认核验单 {v1}，登记整单交接 {h1}")
    status, body = call("POST", "/api/verifications", {
        "verification_no": v1, "batch_nos": ["RT001", "RT002", "RT003"],
    })
    assert status == 201, body
    status, doc = call("POST", f"/api/verifications/{v1}/confirm")
    assert status == 200 and doc["status"] == "CONFIRMED"
    revision = doc["revision"]
    status, ho = call("POST", "/api/handovers", {
        "handover_no": h1, "verification_no": v1, "expected_revision": revision,
    })
    assert status == 201, ho
    print(f"  交接 {h1} 成功：{ho['batch_count']} 批，冻结修订号 v{ho['verification_revision']}，"
          f"确认引用 confirmation_id={ho['confirmation_id']}")

    section(f"3. 部分退回：退回号 {rt1} 退回 [RT002, RT001]（RT003 不发运名额不释放）")
    status, body = call("POST", "/api/returns", {
        "return_no": rt1, "handover_no": h1,
        "reason": "客户拒收：外包装破损，整托退回",
        "batch_nos": ["RT002", "RT001"],
    })
    show("部分退回成功（201，冻结原交接引用与逐批交接快照副本）", status, body)
    assert status == 201
    assert body["verification_no"] == v1
    assert body["verification_revision"] == revision
    assert body["confirmation_id"] == ho["confirmation_id"]
    assert [b["batch_no"] for b in body["batches"]] == ["RT002", "RT001"]
    assert [b["handover_position"] for b in body["batches"]] == [2, 1]
    assert all(b["adjudication"]["disposition"] == "RELEASE" for b in body["batches"])

    for bn in ("RT001", "RT002", "RT003"):
        status, trace = call("GET", f"/api/batches/{bn}/handover")
        state = f"仍占用（{trace['handover']['handover_no']}）" if trace["shipped"] else "已释放"
        print(f"  {bn}: 发运占用 -> {state}")
    status, trace = call("GET", "/api/batches/RT003/handover")
    assert trace["shipped"] is True  # 未退回批次仍占用发运名额
    status, trace = call("GET", "/api/batches/RT001/handover")
    assert trace["shipped"] is False  # 退回批次已释放

    section("4. 不可变核对：原交接详情仍显示当时完整清单，逐批标注退回")
    status, detail = call("GET", f"/api/handovers/{h1}")
    assert [b["batch_no"] for b in detail["batches"]] == ["RT001", "RT002", "RT003"]
    assert detail["returned_batch_count"] == 2 and detail["fully_returned"] is False
    by_no = {b["batch_no"]: b for b in detail["batches"]}
    assert by_no["RT001"]["returned"] is True
    assert by_no["RT001"]["return"]["return_no"] == rt1
    assert by_no["RT003"]["returned"] is False
    assert all(b["adjudication"]["version"] == 1 for b in detail["batches"])
    print(f"  交接 {h1} 详情：batch_count={detail['batch_count']}（当时完整清单不变），"
          f"已退回 {detail['returned_batch_count']} 批，returns={[r['return_no'] for r in detail['returns']]}")
    status, doc_after = call("GET", f"/api/verifications/{v1}")
    assert doc_after["status"] == "CONFIRMED" and doc_after["revision"] == revision
    print(f"  核验单 {v1} 状态/修订号不变：{doc_after['status']} v{doc_after['revision']}")

    section("5. 整体拒绝：重复退回号 / 未知交接 / 空原因 / 重复批次 / 非本交接 / 已退回")
    status, body = call("POST", "/api/returns", {
        "return_no": rt1, "handover_no": h1, "reason": "重复退回号", "batch_nos": ["RT003"],
    })
    print(f"[{status}] 重复退回号 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_RETURN"

    status, body = call("POST", "/api/returns", {
        "return_no": f"RT-X-{suffix}", "handover_no": "NO-SUCH-HANDOVER",
        "reason": "未知交接", "batch_nos": ["RT001"],
    })
    print(f"[{status}] 未知交接 -> {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "HANDOVER_NOT_FOUND"

    status, body = call("POST", "/api/returns", {
        "return_no": f"RT-X-{suffix}", "handover_no": h1, "reason": "   ",
        "batch_nos": ["RT003"],
    })
    print(f"[{status}] 空原因 -> {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD"

    status, body = call("POST", "/api/returns", {
        "return_no": f"RT-X-{suffix}", "handover_no": h1, "reason": "清单内重复",
        "batch_nos": ["RT003", "RT003"],
    })
    print(f"[{status}] 清单内重复批次 -> {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "DUPLICATE_BATCH_IN_RETURN"

    status, body = call("POST", "/api/returns", {
        "return_no": f"RT-X-{suffix}", "handover_no": h1, "reason": "混入非本交接批次",
        "batch_nos": ["RT003", "OTHER-BATCH"],
    })
    print(f"[{status}] 非本交接批次 -> {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "BATCH_NOT_IN_HANDOVER"

    status, body = call("POST", "/api/returns", {
        "return_no": rt2, "handover_no": h1, "reason": "混入已退回批次",
        "batch_nos": ["RT003", "RT001"],
    })
    print(f"[{status}] 含已退回批次 -> {body['error']['code']} "
          f"(details: {[(d['batch_no'], d['return_no']) for d in body['error']['details']]})")
    assert status == 409 and body["error"]["code"] == "BATCH_ALREADY_RETURNED"
    assert body["error"]["details"][0]["return_no"] == rt1
    # 不留下部分释放：RT003 仍被原交接占用
    status, trace = call("GET", "/api/batches/RT003/handover")
    assert trace["shipped"] is True
    print("  上述失败均未留下部分释放：RT003 仍被原交接占用")

    section("6. 退回批次再次发运：新核验单完成当前版本确认 -> 新交接")
    # 未退回的 RT003 仍被 h1 占用：含它的新交接整单拒绝
    status, body = call("POST", "/api/verifications", {
        "verification_no": v2, "batch_nos": ["RT003"],
    })
    assert status == 201, body
    status, doc2 = call("POST", f"/api/verifications/{v2}/confirm")
    assert status == 200
    status, body = call("POST", "/api/handovers", {
        "handover_no": f"RTH-X-{suffix}", "verification_no": v2,
        "expected_revision": doc2["revision"],
    })
    print(f"[{status}] 未退回批次 RT003 随新单交接 -> {body['error']['code']}（仍被 {h1} 占用）")
    assert status == 409 and body["error"]["code"] == "BATCH_ALREADY_SHIPPED"

    # 退回的 RT001 纳入新核验单 -> 确认 -> 新交接成功
    status, body = call("POST", "/api/verifications", {
        "verification_no": v3, "batch_nos": ["RT001"],
    })
    assert status == 201, body
    status, doc3 = call("POST", f"/api/verifications/{v3}/confirm")
    assert status == 200 and doc3["status"] == "CONFIRMED"
    status, ho2 = call("POST", "/api/handovers", {
        "handover_no": h2, "verification_no": v3, "expected_revision": doc3["revision"],
    })
    assert status == 201, ho2
    print(f"  退回批次 RT001 经新核验单 {v3}（当前版本确认）+ 新交接 {h2} 再次发运成功")

    section("7. 追溯：按批次查历次交接、退回及当前去向；退回列表/详情")
    status, trace = call("GET", "/api/batches/RT001/handovers")
    print(f"  RT001 历次交接："
          f"{[(h['handover_no'], h['returned']) for h in trace['handovers']]}，"
          f"当前去向 -> {trace['current']['handover_no']}")
    assert [h["handover_no"] for h in trace["handovers"]] == [h1, h2]
    assert trace["handovers"][0]["return"]["return_no"] == rt1
    assert trace["current"]["handover_no"] == h2

    status, trace = call("GET", "/api/batches/RT003/handovers")
    print(f"  RT003 历次交接："
          f"{[(h['handover_no'], h['returned']) for h in trace['handovers']]}，"
          f"当前去向 -> {trace['current']['handover_no']}（未退回，仍占用）")

    status, body = call("GET", f"/api/returns?handover_no={h1}")
    print(f"[{status}] 交接 {h1} 的退回记录：{[r['return_no'] for r in body['returns']]}")
    status, body = call("GET", f"/api/returns/{rt1}")
    print(f"[{status}] 退回详情 {rt1}：{body['batch_count']} 批，原因「{body['reason']}」，"
          f"冻结确认引用 confirmation_id={body['confirmation_id']}")

    print("\n全部交接退回流程演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

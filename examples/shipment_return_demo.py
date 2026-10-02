#!/usr/bin/env python3
"""食品换线放行服务 —— 已装车交接批次退货端到端调用示例（仅标准库）。

演示流程（独立产线 LRT，交接号/退回号/核验单号带时间戳，可重复运行）：
  1. 登记原料批号与 3 个全部 RELEASE/SHIPPABLE 的批次 RT001/RT002/RT003
  2. 创建核验单 [RT001, RT002, RT003] 并确认，完成装车交接 H（整单发运、逐批占用）
  3. 部分退回：凭原交接号 + 唯一退回号 + 非空原因登记退回 [RT002]
       -> 冻结对原交接与逐批快照的引用，仅释放 RT002；RT001/RT003 仍占用名额
       -> 未全部退回时同一张已确认单据再交接被 BATCH_ALREADY_SHIPPED 拦截
  4. 拒绝场景（整体拒绝、不留部分释放）：
       重复退回号(409 DUPLICATE_RETURN)、已退回批次(422 BATCH_ALREADY_RETURNED)、
       非本交接/未知批次(422 BATCH_NOT_IN_HANDOVER)、未知交接(404)、
       空原因(400)、清单重复(400)
  5. 再发运规则：退回批次必须纳入【新核验单】完成当前版本确认
       -> 全部退回后原单据再交接 409 VERIFICATION_ALREADY_HANDED_OVER
       -> 新核验单确认 + 新交接成功；交接后更正不影响原退回冻结引用
  6. 查询：按批次历次交接/退回与当前去向、原交接详情（完整清单 + 逐批退回标记）、
     按原交接的退回列表、全量退回列表、单笔退回详情（逐批冻结快照引用）

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
        print(f"  已登记原料批号 {lot_no}")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> None:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        print(f"  已登记批次 {batch_no} => "
              f"{body['adjudication']['disposition']} / {body['shipment_eligibility']['status']}")
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
    h1 = f"RT-H-{suffix}"
    r1 = f"RT-R1-{suffix}"
    r2 = f"RT-R2-{suffix}"

    section("1. 准备数据：3 个全部 RELEASE/SHIPPABLE 的批次 RT001/RT002/RT003")
    ensure_lot("RT-RM-MILK", "退货示例奶粉", ["milk"])
    for bn in ("RT001", "RT002", "RT003"):
        ensure_batch(bn, {
            "batch_no": bn, "line_id": LINE,
            "material_lots": ["RT-RM-MILK"],
            "declared_allergens": ["milk"], "cleaned_allergens": [],
        })

    section(f"2. 创建核验单 {v1}，确认后完成装车交接 {h1}（三批整单发运）")
    assert call("POST", "/api/verifications", {
        "verification_no": v1, "batch_nos": ["RT001", "RT002", "RT003"],
    })[0] == 201
    status, doc = call("POST", f"/api/verifications/{v1}/confirm")
    assert status == 200 and doc["status"] == "CONFIRMED"
    revision = doc["revision"]
    status, ho = call("POST", "/api/handovers", {
        "handover_no": h1, "verification_no": v1, "expected_revision": revision,
    })
    assert status == 201, ho
    print(f"  交接 {h1} 完成（冻结修订号 v{revision}），三批发运名额已占用")

    section(f"3. 部分退回：{r1} 退回 [RT002]（凭原交接号 + 非空原因）")
    status, body = call("POST", f"/api/handovers/{h1}/returns", {
        "return_no": r1, "batch_nos": ["RT002"], "reason": "运输途中包装破损，客户拒收",
    })
    show("退回成功（201，逐批引用原交接顺序号与冻结快照）", status, body)
    assert status == 201
    assert body["batch_count"] == 1
    item = body["batches"][0]
    assert item["position"] == 2 and item["batch_no"] == "RT002"
    assert item["handover_snapshot"]["adjudication"]["version"] == 1
    assert item["handover_snapshot"]["shipment_eligibility"]["status"] == "SHIPPABLE"

    status, trace = call("GET", "/api/batches/RT002/handover")
    print(f"  RT002 当前去向：{trace['current_whereabouts']['state']}（已释放，可重新发运）")
    assert trace["shipped"] is False
    status, trace = call("GET", "/api/batches/RT001/handover")
    print(f"  RT001 当前去向：{trace['current_whereabouts']['state']} "
          f"-> {trace['current_whereabouts']['handover_no']}（未退回仍占名额）")
    assert trace["shipped"] is True

    section("4. 整体拒绝：重复退回号 / 已退回 / 非本交接 / 未知交接 / 空原因 / 清单重复")
    status, body = call("POST", f"/api/handovers/{h1}/returns", {
        "return_no": r1, "batch_nos": ["RT001"], "reason": "重复退回号",
    })
    print(f"[{status}] 重复退回号 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_RETURN"

    status, body = call("POST", f"/api/handovers/{h1}/returns", {
        "return_no": r2, "batch_nos": ["RT002", "RT001", "RT-GHOST"], "reason": "混合问题清单",
    })
    print(f"[{status}] 已退回 RT002 + 非本交接 RT-GHOST -> {body['error']['code']}")
    for d in body["error"]["details"]:
        print(f"    - {d['batch_no']}: {d['reason_code']}")
    assert status == 422 and body["error"]["code"] == "RETURN_NOT_ALLOWED"
    codes = {d["batch_no"]: d["reason_code"] for d in body["error"]["details"]}
    assert codes == {"RT002": "BATCH_ALREADY_RETURNED", "RT-GHOST": "BATCH_NOT_IN_HANDOVER"}
    # 整体拒绝：RT001 未被部分释放
    assert call("GET", "/api/batches/RT001/handover")[1]["shipped"] is True

    status, body = call("POST", "/api/handovers/RT-NO-SUCH-H/returns", {
        "return_no": f"{r2}-x", "batch_nos": ["RT001"], "reason": "未知交接",
    })
    print(f"[{status}] 未知交接 -> {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "HANDOVER_NOT_FOUND"

    status, body = call("POST", f"/api/handovers/{h1}/returns", {
        "return_no": f"{r2}-y", "batch_nos": ["RT001"], "reason": "   ",
    })
    print(f"[{status}] 空原因 -> {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD"

    status, body = call("POST", f"/api/handovers/{h1}/returns", {
        "return_no": f"{r2}-z", "batch_nos": ["RT001", "RT001"], "reason": "清单重复",
    })
    print(f"[{status}] 清单内重复 -> {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "DUPLICATE_BATCH_IN_RETURN"

    section("5. 再发运规则：未退完原单据不能复用；全部退回后须新核验单重新确认")
    # RT001/RT003 未退回：原单据再交接先被占用规则拦截
    status, body = call("POST", "/api/handovers", {
        "handover_no": f"{h1}-X", "verification_no": v1, "expected_revision": revision,
    })
    print(f"[{status}] 未退完时原单据再交接 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "BATCH_ALREADY_SHIPPED"

    # 退回剩余两批
    status, body = call("POST", f"/api/handovers/{h1}/returns", {
        "return_no": r2, "batch_nos": ["RT001", "RT003"], "reason": "剩余批次整车退回",
    })
    assert status == 201, body
    print(f"  {r2} 退回 RT001/RT003 成功；至此原交接三批全部退回")

    # 全部退回后：同一张已确认单据仍不得再次交接
    status, body = call("POST", "/api/handovers", {
        "handover_no": f"{h1}-Y", "verification_no": v1, "expected_revision": revision,
    })
    print(f"[{status}] 全退后原单据再交接 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_ALREADY_HANDED_OVER"

    # 退回批次纳入【新核验单】完成当前版本确认后按新单据交接
    assert call("POST", "/api/verifications", {
        "verification_no": v2, "batch_nos": ["RT002"],
    })[0] == 201
    status, doc2 = call("POST", f"/api/verifications/{v2}/confirm")
    assert status == 200
    h2 = f"RT-H2-{suffix}"
    status, body = call("POST", "/api/handovers", {
        "handover_no": h2, "verification_no": v2, "expected_revision": doc2["revision"],
    })
    show(f"RT002 经新核验单 {v2} 重新确认后再次发运（交接 {h2}）", status, body)
    assert status == 201 and body["handover_no"] == h2

    # 交接后的更正不改写原交接与原退回冻结引用
    call("PUT", "/api/batches/RT002", {"declared_allergens": ["milk", "egg"]})
    status, original_return = call("GET", f"/api/returns/{r1}")
    snap = original_return["batches"][0]["handover_snapshot"]
    print(f"  交接后更正：退回 {r1} 内联引用快照仍为 v{snap['adjudication']['version']} "
          f"{snap['adjudication']['disposition']}（冻结不改写）")
    assert snap["adjudication"]["version"] == 1

    section("6. 查询：批次历次交接/退回、原交接完整清单、退回列表与详情")
    status, trace = call("GET", "/api/batches/RT002/handover")
    print(f"  RT002 历次去向：{[(x['handover_no'], '已退:' + x['return']['return_no'] if x['returned'] else '未退回') for x in trace['handovers']]}")
    assert [(x["handover_no"], x["returned"]) for x in trace["handovers"]] == [
        (h1, True), (h2, False),
    ]

    status, ho_detail = call("GET", f"/api/handovers/{h1}")
    print(f"  原交接 {h1} 详情仍是完整清单：{[b['batch_no'] for b in ho_detail['batches']]}")
    print(f"    逐批退回标记：{[(b['batch_no'], b['returned'], (b['return'] or {}).get('return_no')) for b in ho_detail['batches']]}")
    print(f"    汇总：共 {ho_detail['batch_count']} 批，已退 {ho_detail['returned_batch_count']}，"
          f"未退 {ho_detail['outstanding_batch_count']}")
    assert ho_detail["batch_count"] == 3 and ho_detail["returned_batch_count"] == 3

    status, lst = call("GET", f"/api/handovers/{h1}/returns")
    print(f"  原交接退回记录：{[x['return_no'] for x in lst['returns']]}")
    status, all_returns = call("GET", "/api/returns")
    print(f"  全量退回记录：{[x['return_no'] for x in all_returns['returns']]}")
    status, detail = call("GET", f"/api/returns/{r2}")
    print(f"  {r2} 详情：{detail['reason']}，成员 "
          f"{[(b['position'], b['batch_no']) for b in detail['batches']]}，"
          f"引用原单据 {detail['verification_no']} v{detail['verification_revision']}")

    print("\n全部交接批次退货流程演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""复现：逐港卸箱按剩余力矩量级放大容差导致越界被接纳（1e9 量级）。

4 箱 4 独立堆栈（以唯一 重量=槽位承重 强制配载一一对应）：
  CX 港1：重 1、臂 50，力矩 +50          -> 槽 X01（承重 1）
  CA 港1：重 2、臂 -25，力矩 -50         -> 槽 A01（承重 2）
  CL 港2：重 5、臂 200000000，力矩 1e9   -> 槽 L01（承重 5）
  CQ 港2：重 10、臂 0，力矩 0            -> 槽 Q01（承重 10）
初始力矩 1_000_000_000。
港1 窗口 [999_999_950, 1_000_000_000]：合规顺序 CX(剩 999999950)→CA(剩 1e9)；
若先卸 CA（力矩 -50），剩余 = 1000000050 > 港1 上界 1e9，越界 50。
港2 窗口 [0, 2e9]：卸 CL 剩 0，再卸 CQ 仍为 0。
"""

import pytest

from app.unloader import (
    InfeasibleUnloading,
    PlacedContainer,
    PortWindow,
    solve_unloading,
)


def _slots(cl_arm=200000000.0):
    return {
        "slots": [
            {"code": "X01", "stack": "X", "tier": 1, "capacity": 1, "arm": 50.0},
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 2, "arm": -25.0},
            {"code": "L01", "stack": "L", "tier": 1, "capacity": 5,
             "arm": cl_arm},
            {"code": "Q01", "stack": "Q", "tier": 1, "capacity": 10, "arm": 0.0},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -2_000_000_000,
        "max_moment": 2_000_000_000,
    }


MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "CA", "weight": 2, "hazmat": None, "discharge_order": 1},
        {"no": "CX", "weight": 1, "hazmat": None, "discharge_order": 1},
        {"no": "CL", "weight": 5, "hazmat": None, "discharge_order": 2},
        {"no": "CQ", "weight": 10, "hazmat": None, "discharge_order": 2},
    ]
}

PORTS = {
    "ports": [
        {"discharge_order": 1, "min_remaining_moment": 999_999_950,
         "max_remaining_moment": 1_000_000_000},
        {"discharge_order": 2, "min_remaining_moment": 0,
         "max_remaining_moment": 2_000_000_000},
    ]
}


def _post_infra(client, slots=None, manifest=MANIFEST_PAYLOAD, ports=PORTS):
    sr = client.post("/api/slot-revisions", json=slots or _slots()) \
        .json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=manifest) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    cr = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert cr.status_code == 200, cr.text
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=ports)
    assert r.status_code == 201, r.text
    return plan


def test_execute_over_boundary_by_50_rejected(client):
    plan = _post_infra(client)
    pid = plan["plan_id"]

    # 计划与推荐只能从合规首箱 CX 开始（CA 先卸会到 1000000050，越界）
    g0 = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g0["initial_moment"] == 1_000_000_000
    assert [s["container_no"] for s in g0["recommendation"]] == [
        "CX", "CA", "CL", "CQ"
    ]

    # 先卸 CA：剩余 1000000050，越过港1 上界 1000000000（越界量 50）
    r = client.post(
        f"/api/plans/{pid}/unloading/execute",
        json={"container_no": "CA", "operation_no": "OP-REPRO",
              "expected_version": 0},
    )
    assert r.status_code == 422, r.text
    assert "剩余总力矩" in r.json()["detail"]

    # 步骤、进度版本与调整历史均不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["progress_version"] == 0
    assert g["steps"] == []
    assert g["frozen"] is False
    assert g["window_version"] == 0 and g["window_adjustments"] == []

    # 合法端点仍可通过：卸 CX 后剩余恰为 999999950（=下界）
    ok = client.post(
        f"/api/plans/{pid}/unloading/execute",
        json={"container_no": "CX", "operation_no": "OP-LO",
              "expected_version": 0},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["remaining_moment"] == 999_999_950
    assert ok.json()["progress_version"] == 1

    # 再卸 CA：剩余恰为 1000000000（=港1 上界，闭区间端点）
    ok2 = client.post(
        f"/api/plans/{pid}/unloading/execute",
        json={"container_no": "CA", "operation_no": "OP-HI",
              "expected_version": 1},
    )
    assert ok2.status_code == 201, ok2.text
    assert ok2.json()["remaining_moment"] == 1_000_000_000


def test_adjust_window_containment_strict_at_1e9_scale(client):
    plan = _post_infra(client)
    pid = plan["plan_id"]
    # 先合法卸 CX（剩 999999950），执行开始
    r0 = client.post(
        f"/api/plans/{pid}/unloading/execute",
        json={"container_no": "CX", "operation_no": "OP-START",
              "expected_version": 0},
    )
    assert r0.status_code == 201, r0.text

    # 新区间上界 = 1000000050，超出原生效上界 1000000000 达 50：
    # 旧实现 UNLOAD_EPS*scale = 100 的松弛会把它当作“包含”，必须拒绝
    r = client.post(
        f"/api/plans/{pid}/unloading/window-adjustments",
        json={
            "application_no": "ADJ-REPRO",
            "expected_progress_version": 1,
            "expected_window_version": 0,
            "ports": [
                {"discharge_order": 1, "min_remaining_moment": 999_999_950,
                 "max_remaining_moment": 1_000_000_050},
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert "包含于" in r.json()["detail"]

    # 下界低于原生效下界 50 同样拒绝（旧实现 ±100 松弛会误接纳）
    r2 = client.post(
        f"/api/plans/{pid}/unloading/window-adjustments",
        json={
            "application_no": "ADJ-REPRO2",
            "expected_progress_version": 1,
            "expected_window_version": 0,
            "ports": [
                {"discharge_order": 1, "min_remaining_moment": 999_999_900,
                 "max_remaining_moment": 1_000_000_000},
            ],
        },
    )
    assert r2.status_code == 422, r2.text
    assert "包含于" in r2.json()["detail"]

    # 区间版本/调整历史不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0 and g["window_adjustments"] == []

    # 端点完全相等：[999999950, 1000000000] 与原生效区间相同 -> 合法收紧
    eq = client.post(
        f"/api/plans/{pid}/unloading/window-adjustments",
        json={
            "application_no": "ADJ-EQ",
            "expected_progress_version": 1,
            "expected_window_version": 0,
            "ports": [
                {"discharge_order": 1, "min_remaining_moment": 999_999_950,
                 "max_remaining_moment": 1_000_000_000},
            ],
        },
    )
    assert eq.status_code == 201, eq.text
    assert eq.json()["window_version"] == 1


def test_adjust_current_moment_membership_strict_at_1e9_scale(client):
    """收紧后当前港区间不含当前剩余力矩时必须拒绝（旧实现放大容差会放过）。

    本组初始力矩 999999950（CL 力矩改为 999999950），港1 窗口
    [999999900, 1000000000]：先卸 CA（-50）剩余恰为 1e9（=上界，合法）。
    再申请把上界收到 999999999：新区间含于原区间，但当前剩余 1e9 越界 1。
    """
    ports = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": 999_999_900,
         "max_remaining_moment": 1_000_000_000},
        {"discharge_order": 2, "min_remaining_moment": 0,
         "max_remaining_moment": 2_000_000_000},
    ]}
    plan = _post_infra(client, slots=_slots(cl_arm=199999990.0), ports=ports)
    pid = plan["plan_id"]

    r0 = client.post(
        f"/api/plans/{pid}/unloading/execute",
        json={"container_no": "CA", "operation_no": "OP-S", "expected_version": 0},
    )
    assert r0.status_code == 201, r0.text
    assert r0.json()["remaining_moment"] == 1_000_000_000

    # 旧实现 EPS*cscale=100 会把“当前剩余 1e9、新上界 999999999（越界 1）”
    # 误判为落入；修复后必须 422，且原因是当前剩余力矩不在新区间内
    bad = client.post(
        f"/api/plans/{pid}/unloading/window-adjustments",
        json={
            "application_no": "ADJ-CUR",
            "expected_progress_version": 1,
            "expected_window_version": 0,
            "ports": [
                {"discharge_order": 1, "min_remaining_moment": 999_999_900,
                 "max_remaining_moment": 999_999_999},
            ],
        },
    )
    assert bad.status_code == 422, bad.text
    assert "当前剩余总力矩" in bad.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0 and g["window_adjustments"] == []

    # 新区间上界恰为当前剩余 1e9（端点相等）：成员检查通过，仅因后续不可行
    # 才 422（CX 卸后 999999950 不在 {1e9}），拒绝原因不得是当前力矩越界
    ok = client.post(
        f"/api/plans/{pid}/unloading/window-adjustments",
        json={
            "application_no": "ADJ-CUR-OK",
            "expected_progress_version": 1,
            "expected_window_version": 0,
            "ports": [
                {"discharge_order": 1, "min_remaining_moment": 1_000_000_000,
                 "max_remaining_moment": 1_000_000_000},
            ],
        },
    )
    assert ok.status_code == 422, ok.text
    assert "当前剩余总力矩" not in ok.json()["detail"]


def test_solver_recommendation_never_counts_over_boundary_step():
    """前瞻求解器：唯一港1箱先卸会越界 50 时，必须判不可行，不得给出推荐。"""
    # 执行中状态：当前剩余 1e9，剩余箱 CA(-50,港1)、CL(1000000050,港2)、CQ(0,港2)
    boxes = [
        PlacedContainer("CA", weight=2, arm=-25, stack="A", tier=1,
                        discharge_order=1),
        PlacedContainer("CL", weight=5, arm=200000010, stack="L", tier=1,
                        discharge_order=2),
        PlacedContainer("CQ", weight=10, arm=0, stack="Q", tier=1,
                        discharge_order=2),
    ]
    windows = [
        PortWindow(1, 999_999_950, 1_000_000_000),
        PortWindow(2, 0, 2_000_000_000),
    ]
    assert sum(b.weight * b.arm for b in boxes) == 1_000_000_000
    with pytest.raises(InfeasibleUnloading):
        solve_unloading(boxes, windows)

    # 对照：把港1 上界放到 1000000050 后同一卸法可行（端点 1000000050）
    windows_ok = [
        PortWindow(1, 999_999_950, 1_000_000_050),
        PortWindow(2, 0, 2_000_000_000),
    ]
    result = solve_unloading(boxes, windows_ok)
    assert [s.container_no for s in result.steps] == ["CA", "CL", "CQ"]
    assert result.steps[0].remaining_moment == 1_000_000_050

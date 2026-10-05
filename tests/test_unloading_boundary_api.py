"""逐港卸箱执行：剩余力矩边界判定严格化（不随量级放大容差）回归测试。

场景量级 1e9：初始剩余总力矩 1000000000，港 1 生效区间
[999999950, 1000000000]。旧实现按 `EPS * 量级` 放量差（约 100），会把
越出端点 50 的现场卸箱记为成功；修复后按登记值（6 位小数）对生效闭区间
严格判定：越界拒绝且进度不变，恰在端点上仍通过。
"""
from app import models
from app.database import SessionLocal


# C-A1(50,臂1,力矩50,港1)->A1、C-A2(50,臂-1,力矩-50,港1)->B1、
# C-B(100,臂1e7,力矩1e9,港2)->C1；初始总力矩 1e9
SLOT_PAYLOAD = {
    "slots": [
        {"code": "A1", "stack": "A", "tier": 1, "capacity": 100, "arm": 1.0},
        {"code": "B1", "stack": "B", "tier": 1, "capacity": 100, "arm": -1.0},
        {"code": "C1", "stack": "C", "tier": 1, "capacity": 200, "arm": 10000000.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -2000000000,
    "max_moment": 2000000000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C-A1", "weight": 50, "hazmat": None, "discharge_order": 1},
        {"no": "C-A2", "weight": 50, "hazmat": None, "discharge_order": 1},
        {"no": "C-B", "weight": 100, "hazmat": None, "discharge_order": 2},
    ]
}

PORTS = {
    "ports": [
        {"discharge_order": 1, "min_remaining_moment": 999999950,
         "max_remaining_moment": 1000000000},
        {"discharge_order": 2, "min_remaining_moment": 0,
         "max_remaining_moment": 1000000000},
    ]
}


def _seed_and_confirm(client, ports=PORTS):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    assert plan["total_moment"] == 1000000000
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=ports)
    assert r.status_code == 201, r.text
    # 预案：C-A1(999999950) -> C-A2(1000000000) -> C-B(0)，全程恰走端点
    assert [s["container_no"] for s in r.json()["steps"]] == ["C-A1", "C-A2", "C-B"]
    return plan


def _execute(client, plan_id, container, op, version):
    return client.post(
        f"/api/plans/{plan_id}/unloading/execute",
        json={
            "container_no": container,
            "operation_no": op,
            "expected_version": version,
        },
    )


def _adjust(client, plan_id, application_no, ports, *,
            expected_progress_version, expected_window_version):
    return client.post(
        f"/api/plans/{plan_id}/unloading/window-adjustments",
        json={
            "application_no": application_no,
            "expected_progress_version": expected_progress_version,
            "expected_window_version": expected_window_version,
            "ports": ports,
        },
    )


def _execution(client, plan_id):
    return client.get(f"/api/plans/{plan_id}/unloading/execution").json()


# ---------- 卸箱登记：越界拒绝、端点通过 ----------


def test_execute_beyond_effective_window_rejected_422_state_unchanged(client):
    """剩余 1e9、区间 [999999950,1e9] 时先卸力矩 -50 的箱得 1000000050：
    旧容差（约 100）曾接纳，修复后 422 且步骤/进度版本/调整历史不变。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]

    r = _execute(client, pid, "C-A2", "OP-BAD", 0)
    assert r.status_code == 422
    assert "不在当前港" in r.json()["detail"]
    assert "1000000050" in r.json()["detail"]

    g = _execution(client, pid)
    assert g["progress_version"] == 0
    assert g["steps"] == []
    assert g["frozen"] is False
    assert g["window_version"] == 0
    assert g["window_adjustments"] == []
    # 不留任何执行痕迹（首次登记未发生，操作台账为空）
    db = SessionLocal()
    try:
        assert db.query(models.UnloadingExecution).count() == 0
        assert db.query(models.UnloadingExecOperation).count() == 0
    finally:
        db.close()


def test_execute_exact_window_endpoints_accepted(client):
    """卸后剩余力矩恰落在生效区间下界/上界：合法端点仍可通过。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]

    # 卸 C-A1：剩余 999999950 == 区间下界
    r1 = _execute(client, pid, "C-A1", "OP-1", 0)
    assert r1.status_code == 201, r1.text
    assert r1.json()["remaining_moment"] == 999999950

    # 卸 C-A2：剩余 1000000000 == 区间上界
    r2 = _execute(client, pid, "C-A2", "OP-2", 1)
    assert r2.status_code == 201, r2.text
    assert r2.json()["remaining_moment"] == 1000000000

    # 卸 C-B：剩余 0，完结
    r3 = _execute(client, pid, "C-B", "OP-3", 2)
    assert r3.status_code == 201, r3.text
    assert r3.json()["remaining_moment"] == 0
    assert r3.json()["completed"] is True


def test_rejected_registration_keeps_progress_and_steps(client):
    """执行中途的非法登记被拒绝后：步骤、进度版本与剩余力矩均不变。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C-A1", "OP-1", 0).status_code == 201  # 999999950
    # C-B 属港2，非当前港 -> 422；进度不变
    r = _execute(client, pid, "C-B", "OP-SKIP", 1)
    assert r.status_code == 422
    g = _execution(client, pid)
    assert g["progress_version"] == 1
    assert g["remaining_moment"] == 999999950
    assert [s["container_no"] for s in g["steps"]] == ["C-A1"]


# ---------- 区间收紧：新区间必须包含于原生效区间 ----------


def test_adjust_window_beyond_effective_rejected_422(client):
    """新区间上界超出生效上界 50（旧容差 100 曾接纳）-> 422，版本/历史不变。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C-A1", "OP-1", 0).status_code == 201
    assert _execute(client, pid, "C-A2", "OP-2", 1).status_code == 201

    r = _adjust(client, pid, "ADJ-WIDE", [
        {"discharge_order": 2, "min_remaining_moment": 0,
         "max_remaining_moment": 1000000050},
    ], expected_progress_version=2, expected_window_version=0)
    assert r.status_code == 422 and "包含于" in r.json()["detail"]

    # 下界越出同理
    r2 = _adjust(client, pid, "ADJ-WIDE2", [
        {"discharge_order": 2, "min_remaining_moment": -50,
         "max_remaining_moment": 1000000000},
    ], expected_progress_version=2, expected_window_version=0)
    assert r2.status_code == 422 and "包含于" in r2.json()["detail"]

    g = _execution(client, pid)
    assert g["window_version"] == 0 and g["window_adjustments"] == []
    assert g["progress_version"] == 2


def test_adjust_window_equal_to_effective_accepted(client):
    """新区间与生效区间端点重合（包含于的边界情形）合法。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C-A1", "OP-1", 0).status_code == 201
    assert _execute(client, pid, "C-A2", "OP-2", 1).status_code == 201

    r = _adjust(client, pid, "ADJ-EQ", [
        {"discharge_order": 2, "min_remaining_moment": 0,
         "max_remaining_moment": 1000000000},
    ], expected_progress_version=2, expected_window_version=0)
    assert r.status_code == 201, r.text
    assert r.json()["window_version"] == 1
    eff = {p["discharge_order"]: (p["min_remaining_moment"],
                                  p["max_remaining_moment"])
           for p in r.json()["effective_ports"]}
    assert eff[2] == (0, 1000000000)


def test_adjust_current_moment_must_stay_in_current_window(client):
    """调整后当前港区间不含当前剩余力矩（差 1，旧容差曾接纳）-> 422。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C-A1", "OP-1", 0).status_code == 201
    assert _execute(client, pid, "C-A2", "OP-2", 1).status_code == 201
    # 当前剩余 1e9；把港2 上界收紧到 999999999（包含于原区间但不含现状）
    r = _adjust(client, pid, "ADJ-EXCL", [
        {"discharge_order": 2, "min_remaining_moment": 0,
         "max_remaining_moment": 999999999},
    ], expected_progress_version=2, expected_window_version=0)
    assert r.status_code == 422 and "当前剩余总力矩" in r.json()["detail"]
    g = _execution(client, pid)
    assert g["window_version"] == 0 and g["window_adjustments"] == []


# ---------- 前瞻推荐：越界步骤不算可行 ----------


def test_recommendation_never_steps_outside_effective_window(client):
    """执行视图的推荐顺序每步剩余力矩均落在生效闭区间内（端点允许）。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]

    g = _execution(client, pid)
    eff = {p["discharge_order"]: (p["min_remaining_moment"],
                                  p["max_remaining_moment"])
           for p in g["effective_ports"]}
    assert g["recommendation"], "存在可行推荐顺序"
    for s in g["recommendation"]:
        wmin, wmax = eff[s["discharge_order"]]
        assert wmin <= s["remaining_moment"] <= wmax

    # 登记一步后推荐仍全程在区间内
    assert _execute(client, pid, "C-A1", "OP-1", 0).status_code == 201
    g2 = _execution(client, pid)
    assert [s["container_no"] for s in g2["recommendation"]] == ["C-A2", "C-B"]
    for s in g2["recommendation"]:
        wmin, wmax = eff[s["discharge_order"]]
        assert wmin <= s["remaining_moment"] <= wmax


def test_unloading_plan_generation_strict_at_large_magnitude(client):
    """规划阶段同样严格：港1 唯一箱力矩 -50，卸后 1000000050 越界 -> 422 无解。"""
    client.post("/api/slot-revisions", json={
        "slots": [
            {"code": "A1", "stack": "A", "tier": 1, "capacity": 100, "arm": -1.0},
            {"code": "C1", "stack": "C", "tier": 1, "capacity": 200,
             "arm": 10000000.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -2000000000, "max_moment": 2000000000,
    })
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C-A", "weight": 50, "hazmat": None, "discharge_order": 1},
            {"no": "C-C", "weight": 100.000005, "hazmat": None,
             "discharge_order": 2},
        ]
    })
    plan = client.post("/api/plans/compute", json={}).json()
    assert plan["total_moment"] == 1000000000
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 1
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json={
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": 999999950,
             "max_remaining_moment": 1000000000},
            {"discharge_order": 2, "min_remaining_moment": 0,
             "max_remaining_moment": 1000000000},
        ]
    })
    assert r.status_code == 422
    assert "区间" in r.json()["detail"]
    # 无解不保存
    assert client.get(
        f"/api/plans/{plan['plan_id']}/unloading").status_code == 404

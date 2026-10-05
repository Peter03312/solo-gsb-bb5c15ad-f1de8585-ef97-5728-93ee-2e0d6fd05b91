#!/usr/bin/env sh
# 本轮调用示例：修复“按剩余力矩量级放大容差导致越界卸箱被接纳”后的完整 HTTP 链路。
#
# 场景（初始剩余总力矩 = 1,000,000,000，当前港1生效闭区间
# [999,999,950, 1,000,000,000]）：
#   * 箱 CX 力矩 +50：卸后剩 999,999,950（=下界，合法端点）
#   * 箱 CA 力矩 -50：卸后剩 1,000,000,050（越过上界 50，必须 422 拒绝）
#   * 箱 CL 力矩 1,000,000,000（港2）、箱 CQ 力矩 0（港2）
#
# 用法：
#   uvicorn app.main:app --host 127.0.0.1 --port 8000
#   BASE=http://127.0.0.1:8000/api sh examples/unloading_boundary_demo.sh
#
# 依赖 curl 与 python3（仅用于解析 JSON）。需要重置时换用空数据库重启服务。
set -eu

BASE="${BASE:-http://127.0.0.1:8000/api}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
py() { python3 -c 'import sys,json;d=json.load(sys.stdin);'"$1"; }
# req METHOD PATH OUTFILE DATA：打印 HTTP 状态码，响应体写入 $TMP/$3
req() {
  code=$(curl -sS -o "$TMP/$3" -w '%{http_code}' -X "$1" "$BASE$2" \
    -H 'Content-Type: application/json' --data-binary "$4")
  echo "HTTP $code"
}

# 1. 槽位资料（以“重量=唯一槽位承重”强制配载一一对应）
req POST /slot-revisions sr '{
  "slots": [
    {"code":"X01","stack":"X","tier":1,"capacity":1,"arm":50},
    {"code":"A01","stack":"A","tier":1,"capacity":2,"arm":-25},
    {"code":"L01","stack":"L","tier":1,"capacity":5,"arm":200000000},
    {"code":"Q01","stack":"Q","tier":1,"capacity":10,"arm":0}
  ],
  "adjacency": [], "incompatible_classes": [],
  "min_moment": -2000000000, "max_moment": 2000000000
}'
SRN=$(py 'print(d["slots_revision"])' < "$TMP/sr")

# 2. 箱清单
req POST /manifest-revisions mr '{
  "containers": [
    {"no":"CA","weight":2,"hazmat":null,"discharge_order":1},
    {"no":"CX","weight":1,"hazmat":null,"discharge_order":1},
    {"no":"CL","weight":5,"hazmat":null,"discharge_order":2},
    {"no":"CQ","weight":10,"hazmat":null,"discharge_order":2}
  ]
}'
MRN=$(py 'print(d["manifest_revision"])' < "$TMP/mr")

# 3. 配载计算并确认
req POST /plans/compute pc '{}'
PID=$(py 'print(d["plan_id"])' < "$TMP/pc")
req POST "/plans/$PID/confirm" cf "{\"slots_revision\":$SRN,\"manifest_revision\":$MRN}"
echo "plan_id=$PID"

# 4. 生成逐港卸箱顺序：港1 [999999950, 1000000000]，港2 [0, 2000000000]
req POST "/plans/$PID/unloading" seq '{
  "ports": [
    {"discharge_order":1,"min_remaining_moment":999999950,"max_remaining_moment":1000000000},
    {"discharge_order":2,"min_remaining_moment":0,"max_remaining_moment":2000000000}
  ]
}'
echo "-- 前瞻推荐（不得把越界首箱 CA 算作可行）--"
curl -sS "$BASE/plans/$PID/unloading/execution" | py '
print([(s["container_no"], s["remaining_moment"]) for s in d["recommendation"]])'

# 5. 越界登记：先卸 CA（-50），剩余 1,000,000,050 > 1,000,000,000 → 期望 422
echo "-- 越界登记（期望 422）--"
req POST "/plans/$PID/unloading/execute" ex-bad '{
  "container_no":"CA","operation_no":"OP-BOUND-0001","expected_version":0}'
cat "$TMP/ex-bad"; echo

# 6. 步骤、进度版本、调整历史均不变
echo "-- 拒绝后状态（progress_version=0、0 步、window_version=0）--"
curl -sS "$BASE/plans/$PID/unloading/execution" | py '
print("progress_version=", d["progress_version"],
      "steps=", len(d["steps"]),
      "window_version=", d["window_version"],
      "adjustments=", len(d["window_adjustments"]))'

# 7. 合法端点：卸 CX 剩 999999950（=下界）→ 201；卸 CA 剩 1000000000（=上界）→ 201
echo "-- 合法端点登记（期望均 201）--"
req POST "/plans/$PID/unloading/execute" ex1 '{
  "container_no":"CX","operation_no":"OP-BOUND-0002","expected_version":0}'
py 'print("CX ->", d["remaining_moment"])' < "$TMP/ex1"
req POST "/plans/$PID/unloading/execute" ex2 '{
  "container_no":"CA","operation_no":"OP-BOUND-0003","expected_version":1}'
py 'print("CA ->", d["remaining_moment"])' < "$TMP/ex2"

# 8. 安全区间收紧：上界 2000000050 超出原上界 2000000000（50）→ 期望 422
echo "-- 越界收紧（期望 422，新区间必须包含于原生效区间）--"
req POST "/plans/$PID/unloading/window-adjustments" adj-bad '{
  "application_no":"ADJ-BOUND-0001",
  "expected_progress_version":2,"expected_window_version":0,
  "ports":[{"discharge_order":2,"min_remaining_moment":0,"max_remaining_moment":2000000050}]}'
cat "$TMP/adj-bad"; echo

# 9. 合法收紧（端点等于当前值）→ 201
echo "-- 合法收紧港2 到 [0,1000000000]（期望 201）--"
req POST "/plans/$PID/unloading/window-adjustments" adj-ok '{
  "application_no":"ADJ-BOUND-0002",
  "expected_progress_version":2,"expected_window_version":0,
  "ports":[{"discharge_order":2,"min_remaining_moment":0,"max_remaining_moment":1000000000}]}'
py 'print("window_version=", d["window_version"])' < "$TMP/adj-ok"

# 10. 成功操作同操作号同内容重试 → 200，返回首次快照
echo "-- 幂等重试 OP-BOUND-0002（期望 200）--"
req POST "/plans/$PID/unloading/execute" ex-retry '{
  "container_no":"CX","operation_no":"OP-BOUND-0002","expected_version":0}'
py 'print("snapshot progress_version=", d["progress_version"],
      "remaining=", d["remaining_moment"])' < "$TMP/ex-retry"

# 11. 卸完剩余箱
echo "-- 卸 CL（剩 0）、卸 CQ（完结）--"
req POST "/plans/$PID/unloading/execute" ex3 '{
  "container_no":"CL","operation_no":"OP-BOUND-0004","expected_version":2}'
py 'print("CL ->", d["remaining_moment"])' < "$TMP/ex3"
req POST "/plans/$PID/unloading/execute" ex4 '{
  "container_no":"CQ","operation_no":"OP-BOUND-0005","expected_version":3}'
py 'print("completed=", d["completed"], "remaining=", d["remaining_moment"])' < "$TMP/ex4"

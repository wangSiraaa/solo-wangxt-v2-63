# 街道环卫考核 API（Django REST Framework + PostGIS + Pillow pHash）

无界面 REST API，用于街道环卫问题的取证、去重、整改、逾期升级与扣分处罚。

## 核心业务规则

| 要求 | 实现 |
| --- | --- |
| 同一现场问题不重复扣分 | 照片按 **位置（PostGIS 大圆距离 ≤ 50 m）+ 时间（≤ 72 h）+ 同网格同类别 + 事件未结案** 自动归入同一 OPEN 事件；一个事件每个处罚级别只有一个唯一处罚单元（`(event, level)` 唯一约束） |
| 相似照片不合并不同地点 | pHash 只生成 `DuplicateCandidate` 疑似候选；跨地点候选 `within_spatial_window=false`，人工判 `same_event` 会被 409 拒绝；必须人工判定 `different_place` 或 `same_event` |
| 不同角度拍摄不重复计扣 | 角度重拍（旋转/平移/重压缩）自动并入 OPEN 事件；人工确认 `same_event` 后重复事件作废，其临时处罚单元删除 |
| 整改后复发是新事件 | 已 `rectified` 事件不再吸收新照片，新照片创建全新事件与独立处罚链；已结案事件也禁止参与合并 |
| 扣分归属按发生时合同 | 处罚单元在创建时快照 `liable_contract`/`contractor`，用 `event.opened_at`（发生时间，非录入时间）匹配 `[valid_from, valid_to)` |
| 逾期升级基于可注入时钟 | 全部时间判断走 `inspections.clock.Clock`（`POST /api/clock/` 固定/复位），24/48/72h 阶梯 1/2/3 分；扫描幂等（可安全重跑） |
| 复核通过锁定，更正只能追加 | `reviewed_at` 非空即锁：不可重复复核(409)、无 PUT/DELETE 端点；更正只能 `POST .../corrections/` 追加，`corrected_points = 原值 + Σdelta` |
| 重复整改回调 | `Rectification` 追加保存；仅第一条 `effective=true` 并结案，后续全部 `effective=false`，结案时间不被改写 |
| 每笔扣分可追溯 | 每个处罚单元携带：唯一 id、级别、规则版本、责任合同/承包商、原始照片 id 列表（含 pHash 与文件）、更正流水 |

## 模型

- `RoadGrid`：道路网格（PostGIS Polygon, SRID 4326）
- `Contractor` / `CleaningContract`：保洁公司与半开责任区间
- `Photo`：照片（ImageField + 64 位 pHash + Point + `taken_at`）
- `DuplicateCandidate`：pHash 疑似对（汉明距离、米距、秒差、空间/时间窗口命中、人工判定）
- `Event`：去重后的问题事件（状态 open/rectified，合并走 `merged_into`）
- `Rectification`：整改回调（append-only）
- `PenaltyUnit`：唯一处罚单元（`(event, level)` 唯一，复核锁定）
- `PenaltyCorrection`：追加式更正

## pHash（Pillow，纯 Python DCT）

`inspections/phash.py`：灰度 → 32×32 → 2D DCT → 取左上 8×8 → 对 AC 中位数二值化，输出 64 hex。
阈值 `PHASH_HAMMING_THRESHOLD=8`（可环境变量覆盖）。实测：同场景重拍距离 6，不同场景 ≥20，字节相同为 0。

> pHash 只是“疑似重复候选生成器”，**绝不**单独决定事件合并。

## 快速开始

```bash
# 1) Python 依赖（venv 已在 .venv，依赖见 requirements.txt）
.venv/bin/pip install -r requirements.txt

# 2) 数据库（无 root 环境可用脚本经 micromamba 装 PG18+PostGIS3.6 到 /tmp）
./setup_db.sh
source .env.sh          # PGHOST/LD_LIBRARY_PATH/GDAL/PROJ 等

# 3) 模拟图片 + 运行
.venv/bin/python manage.py generate_sample_photos
.venv/bin/python manage.py runserver 127.0.0.1:8000

# 4) OpenAPI（无 UI，原始 YAML/JSON）
.venv/bin/python manage.py spectacular --file openapi.yaml
# 或 http://127.0.0.1:8000/api/schema/
```

## 主要端点

```
POST   /api/grids|contractors|contracts/        主数据（GeoJSON 几何）
POST   /api/photos/                             multipart 上传照片（自动 pHash/建候选/关联事件）
GET    /api/events/?status=&category=&grid=     事件列表
POST   /api/events/{id}/rectify/                整改回调（可带整改照片；重复调用追加）
GET    /api/events/{id}/rectifications/
GET    /api/duplicate-candidates/?decision=&same_spot_only=
POST   /api/duplicate-candidates/{id}/decide/   人工判定 same_event / different_place
POST   /api/escalations/                        按时钟执行逾期升级（幂等）
GET/POST /api/penalty-units/                    处罚台账（?contractor= 过滤）
POST   /api/penalty-units/{id}/review/          复核锁定
POST   /api/penalty-units/{id}/corrections/     追加更正（锁定后也只能走这里）
GET/POST /api/clock/                            测试时钟固定/复位
```

## 测试（含题目要求的三类例子）

```bash
MEDIA_ROOT=/tmp/test_media .venv/bin/python manage.py test inspections
```

9 个用例：

1. `test_same_picture_uploaded_at_different_locations_is_not_merged` — **同图跨地点误传**：hamming=0 但相距 ~10 km，两个事件、两笔基础扣分，强制 same_event 合并被拒绝，可标记 different_place
2. `test_same_event_different_angle_scored_once` — 同点不同角度（hamming=6），自动并入 + 人工确认，只有一笔扣分
3. `test_recurrence_after_rectification_is_a_new_event` — **同地点复发**：整改结案后复发产生新事件与新扣分
4. `test_repeated_rectification_callbacks_are_appended_only` — **重复整改回调**：仅首条生效，全部追加保留
5. `test_escalation_uses_injected_clock_and_is_idempotent` — 23h 不升级、25h L1、50h L2、重复扫描不重发、结案后停止
6. `test_attribution_uses_occurrence_time_contract` — 发生时甲公司，录入/升级时已换乙公司，扣分仍归甲
7. `test_review_locks_unit_and_corrections_append` — 锁定后重复复核 409、DELETE 405、只能追加更正
8. `test_every_deduction_traces_to_unique_unit_and_evidence` — 台账逐行校验唯一 `(event,level)` 与证据链
9. 网格外上传 400

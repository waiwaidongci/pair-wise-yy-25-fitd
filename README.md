# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员和一个含分歧的示例批次。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/batches/{id}/switch-guideline`：管理员把未冻结批次切到新版指南
- `GET /api/batches/{id}/freeze-readiness`：冻结入口检查（当前版本缺哪些条目、哪些分歧未仲裁）
- `POST /api/annotations`、`POST /api/adjudications`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，也不能再换版；导出结果来自不可变的 `gold_records`。

## 指南中途改版（补换版）

- 只有管理员可以对未冻结批次调用 `switch-guideline`；切换记录写入 `guideline_switches`，可在 `/api/state` 中查询。
- 旧标注、旧仲裁按原指南版本保留（`annotations`、`adjudications` 均按 `(条目, 指南版本)` 唯一），同一位标注员可以针对新版再交一份。
- 分歧、一致性、仲裁与冻结默认只认批次当前指南版本；旧版本数据不参与计算但可在条目视图与快照中查看。
- `GET /api/items/{id}` 返回当前指南，以及 `own_annotations_by_guideline`（本人按版本提交的每份标注，含 `is_current` 标记）；`/api/state` 的标注与仲裁同样带版本与 `is_current`。
- 新版还没收齐时，`freeze-readiness` 和冻结接口会列出缺少标注的条目（序号、当前版本已交份数、已交标注员）。
- 切换、补标和冻结结果都保留在原服务中：切换历史与冻结记录见 `/api/state`，冻结后的金标准见 `/api/batches/{id}/gold`。

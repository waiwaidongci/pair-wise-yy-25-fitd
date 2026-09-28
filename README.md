# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员、一位管理员、一个含分歧的示例批次，以及一个可切换的 v2 指南示例。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；中途换版后旧标注保留、同一标注员按新版补标、只按当前版本检查分歧/一致性/冻结、缺项明细以及冻结后禁止换版；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/batches/{id}/guideline`、`GET /api/batches/{id}/guideline-switch`
- `GET /api/batches/{id}/freeze-status`、`GET /api/guideline-switches/{id}`
- `POST /api/annotations`、`POST /api/adjudications`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求当前指南下每个已分配条目收齐所有已分配标注员的新版标注（且至少两份）、没有当前版本未仲裁分歧；冻结入口可通过 freeze-status 查询缺少哪些条目和标注员。管理员只能给未冻结批次换版；换版后旧标注和旧仲裁按原指南版本保留，同一条目的同一位标注员可针对新版再提交一份。分歧、一致性和冻结默认只统计批次当前指南；冻结后不能换版或修改标注，导出结果来自不可变的 `gold_records`。

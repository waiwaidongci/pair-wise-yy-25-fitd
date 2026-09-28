from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


class CorpusDB:
    """A small multi-annotator corpus governance service."""

    def __init__(self, path: str = "corpus.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('annotator','arbitrator','manager'))
            );
            CREATE TABLE IF NOT EXISTS guidelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version TEXT NOT NULL UNIQUE,
              rules TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','annotating','frozen')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL,
              text TEXT NOT NULL,
              UNIQUE(batch_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','submitted')),
              UNIQUE(item_id, annotator_id)
            );
            CREATE TABLE IF NOT EXISTS annotations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(item_id, annotator_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS adjudications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              final_label TEXT NOT NULL,
              reason TEXT NOT NULL,
              arbitrator_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(item_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS discussions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              author_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL,
              contains_answer INTEGER NOT NULL DEFAULT 0 CHECK(contains_answer IN (0,1)),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gold_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              source TEXT NOT NULL CHECK(source IN ('consensus','adjudication')),
              adjudication_id INTEGER REFERENCES adjudications(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_freezes (
              batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
              metrics_json TEXT NOT NULL,
              frozen_by INTEGER NOT NULL REFERENCES users(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guideline_switches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              from_guideline_id INTEGER REFERENCES guidelines(id),
              to_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              switched_by INTEGER NOT NULL REFERENCES users(id),
              switched_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        # 旧库的 adjudications 以 item_id 为唯一键；改为 (item_id, guideline_id)，
        # 让旧版仲裁随旧指南保留、新版可以再仲裁。
        needs_migration = False
        for idx in self.conn.execute("PRAGMA index_list('adjudications')").fetchall():
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in self.conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()]
            if cols == ["item_id"]:
                needs_migration = True
        if needs_migration:
            with self.transaction():
                self.conn.executescript(
                    """
                    CREATE TABLE adjudications_new (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                      guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
                      final_label TEXT NOT NULL,
                      reason TEXT NOT NULL,
                      arbitrator_id INTEGER NOT NULL REFERENCES users(id),
                      created_at TEXT NOT NULL,
                      UNIQUE(item_id, guideline_id)
                    );
                    INSERT INTO adjudications_new(id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at)
                    SELECT id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at FROM adjudications;
                    DROP TABLE adjudications;
                    ALTER TABLE adjudications_new RENAME TO adjudications;
                    """
                )

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        arb = self.add_user("仲裁员", "arbitrator")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        batch = self.create_batch("情感标注示例", guideline)
        item1 = self.add_item(batch, 1, "这个更新让工作流畅了很多。")
        item2 = self.add_item(batch, 2, "功能没有变化，但也没有明显问题。")
        self.assign(item1, a1)
        self.assign(item1, a2)
        self.assign(item2, a1)
        self.assign(item2, a2)
        self.submit_annotation(item1, a1, "正向", "整体表达积极")
        self.submit_annotation(item1, a2, "中性", "描述较克制")
        self.submit_annotation(item2, a1, "中性")
        self.submit_annotation(item2, a2, "中性")

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"annotator", "arbitrator", "manager"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def add_guideline(self, version: str, rules: str) -> int:
        if not version.strip() or not rules.strip():
            raise DomainError("指南版本和规则不能为空")
        with self.transaction():
            cur = self.conn.execute("INSERT INTO guidelines(version,rules) VALUES(?,?)", (version.strip(), rules.strip()))
        return int(cur.lastrowid)

    def create_batch(self, name: str, guideline_id: int) -> int:
        if not name.strip() or not self.conn.execute("SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone():
            raise DomainError("批次名称或指南无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO batches(name,guideline_id,created_at) VALUES(?,?,?)",
                (name.strip(), guideline_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def switch_guideline(self, batch_id: int, guideline_id: int, manager_id: int) -> dict:
        """把未冻结批次切到新版指南：旧标注按原指南保留，分歧/一致性/冻结此后只认新版。"""
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        target = self.conn.execute("SELECT * FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("冻结后不能换版")
        if not target:
            raise DomainError("目标指南不存在或已停用")
        if batch["guideline_id"] == guideline_id:
            raise DomainError("批次已绑定该指南版本")
        old = self.conn.execute("SELECT version FROM guidelines WHERE id=?", (batch["guideline_id"],)).fetchone()
        switched_at = datetime.now().isoformat()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO guideline_switches(batch_id,from_guideline_id,to_guideline_id,switched_by,switched_at) "
                "VALUES(?,?,?,?,?)",
                (batch_id, batch["guideline_id"], guideline_id, manager_id, switched_at),
            )
            self.conn.execute("UPDATE batches SET guideline_id=? WHERE id=?", (guideline_id, batch_id))
        return {
            "batch_id": batch_id,
            "from_version": old["version"] if old else None,
            "to_version": target["version"],
            "switched_at": switched_at,
        }

    def add_item(self, batch_id: int, ordinal: int, text: str) -> int:
        batch = self.conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] == "frozen":
            raise DomainError("批次不存在或已经冻结")
        if ordinal <= 0 or not text.strip():
            raise DomainError("序号必须大于0且文本不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO items(batch_id,ordinal,text) VALUES(?,?,?)", (batch_id, ordinal, text.strip()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("该批次序号已存在") from exc
        return int(cur.lastrowid)

    def assign(self, item_id: int, annotator_id: int) -> int:
        item = self.conn.execute("SELECT batch_id FROM items WHERE id=?", (item_id,)).fetchone()
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (annotator_id,)).fetchone()
        if not item or not user or user["role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        with self.transaction():
            self.conn.execute("UPDATE batches SET status='annotating' WHERE id=? AND status='draft'", (item["batch_id"],))
            try:
                cur = self.conn.execute(
                    "INSERT INTO assignments(batch_id,item_id,annotator_id) VALUES(?,?,?)",
                    (item["batch_id"], item_id, annotator_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一标注员不能重复领取同一条目") from exc
        return int(cur.lastrowid)

    def submit_annotation(self, item_id: int, annotator_id: int, label: str, comment: str = "") -> int:
        if not label.strip():
            raise DomainError("标签不能为空")
        item = self.conn.execute(
            "SELECT i.*, b.guideline_id, b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, annotator_id)
        ).fetchone()
        if not item or not assignment:
            raise DomainError("只能提交已分配条目的标注")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能修改标注")
        with self.transaction():
            self.conn.execute(
                "DELETE FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item_id, item["guideline_id"]),
            )
            try:
                cur = self.conn.execute(
                    "INSERT INTO annotations(item_id,annotator_id,guideline_id,label,comment,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, annotator_id, item["guideline_id"], label.strip(), comment.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE annotations SET label=?,comment=?,created_at=? WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (label.strip(), comment.strip(), datetime.now().isoformat(), item_id, annotator_id, item["guideline_id"]),
                )
                annotation_id = self.conn.execute(
                    "SELECT id FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (item_id, annotator_id, item["guideline_id"]),
                ).fetchone()["id"]
            else:
                annotation_id = int(cur.lastrowid)
            self.conn.execute("UPDATE assignments SET status='submitted' WHERE id=?", (assignment["id"],))
        return int(annotation_id)

    def add_discussion(self, item_id: int, author_id: int, body: str, contains_answer: bool = False) -> int:
        if not body.strip():
            raise DomainError("讨论内容不能为空")
        if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise DomainError("条目不存在")
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (author_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO discussions(item_id,author_id,body,contains_answer,created_at) VALUES(?,?,?,?,?)",
                (item_id, author_id, body.strip(), int(contains_answer), datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def get_item_for_user(self, item_id: int, user_id: int) -> dict:
        item = self.conn.execute(
            "SELECT i.id,i.batch_id,i.ordinal,i.text,b.guideline_id,b.status,"
            "g.version AS guideline_version,g.rules "
            "FROM items i JOIN batches b ON b.id=i.batch_id JOIN guidelines g ON g.id=b.guideline_id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        own_rows = self.conn.execute(
            "SELECT an.id,an.label,an.comment,an.created_at,an.guideline_id,g.version AS guideline_version,"
            "(an.guideline_id=?) AS is_current "
            "FROM annotations an JOIN guidelines g ON g.id=an.guideline_id "
            "WHERE an.item_id=? AND an.annotator_id=? ORDER BY an.guideline_id",
            (item["guideline_id"], item_id, user_id),
        ).fetchall()
        revealed = bool(own_rows)
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id", (item_id,)
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload = dict(item)
        own_versions = [dict(row) for row in own_rows]
        payload["own_annotation"] = next(
            (row for row in own_versions if row["is_current"]), own_versions[0] if own_versions else None
        )
        payload["own_annotations_by_guideline"] = own_versions
        payload["discussions"] = discussions
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        version = self.conn.execute("SELECT version FROM guidelines WHERE id=?", (batch["guideline_id"],)).fetchone()["version"]
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            rows = self.conn.execute(
                "SELECT a.*,u.name FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=? ORDER BY a.id",
                (item["id"], batch["guideline_id"]),
            ).fetchall()
            labels = {row["label"] for row in rows}
            adj = self.conn.execute(
                "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item["id"], batch["guideline_id"]),
            ).fetchone()
            if len(rows) >= 2 and len(labels) > 1 and not adj:
                result.append({
                    "item_id": item["id"], "ordinal": item["ordinal"], "text": item["text"],
                    "guideline_version": version,
                    "labels": [dict(row) for row in rows],
                })
        return result

    def adjudicate(self, item_id: int, final_label: str, reason: str, arbitrator_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        item = self.conn.execute(
            "SELECT i.*,b.guideline_id,b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item or not user or user["role"] != "arbitrator":
            raise DomainError("条目或仲裁员无效")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能重新仲裁")
        rows = self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?",
            (item_id, item["guideline_id"]),
        ).fetchall()
        if len(rows) < 2:
            raise DomainError("当前指南版本至少需要两份标注才能仲裁")
        if not final_label.strip() or len(reason.strip()) < 5:
            raise DomainError("最终标签必填，仲裁理由至少5个字符")
        with self.transaction():
            existing = self.conn.execute(
                "SELECT id FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item_id, item["guideline_id"]),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE adjudications SET final_label=?,reason=?,arbitrator_id=?,created_at=? WHERE id=?",
                    (final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat(), existing["id"]),
                )
                adjudication_id = existing["id"]
            else:
                cur = self.conn.execute(
                    "INSERT INTO adjudications(item_id,guideline_id,final_label,reason,arbitrator_id,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, item["guideline_id"], final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat()),
                )
                adjudication_id = int(cur.lastrowid)
        return int(adjudication_id)

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        per_item, agreement_pairs, total_pairs = [], 0, 0
        label_totals: Counter[str] = Counter()
        all_annotation_count = 0
        for item in items:
            labels = [r["label"] for r in self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchall()]
            if len(labels) < 2:
                per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": None, "annotations": len(labels)})
                continue
            pairs = total = 0
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    total += 1
                    pairs += labels[i] == labels[j]
            agreement = pairs / total
            agreement_pairs += pairs
            total_pairs += total
            all_annotation_count += len(labels)
            label_totals.update(labels)
            per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": round(agreement, 4), "annotations": len(labels)})
        pairwise = agreement_pairs / total_pairs if total_pairs else None
        expected = sum((count / all_annotation_count) ** 2 for count in label_totals.values()) if all_annotation_count else None
        kappa = None
        if pairwise is not None and expected is not None and expected < 1:
            kappa = (pairwise - expected) / (1 - expected)
        return {
            "batch_id": batch_id,
            "guideline_id": batch["guideline_id"],
            "guideline_version": self.conn.execute(
                "SELECT version FROM guidelines WHERE id=?", (batch["guideline_id"],)
            ).fetchone()["version"],
            "items_with_multiple_annotations": total_pairs and sum(1 for row in per_item if row["agreement"] is not None),
            "pairwise_agreement": round(pairwise, 4) if pairwise is not None else None,
            "fleiss_kappa": round(kappa, 4) if kappa is not None else None,
            "items": per_item,
        }

    def freeze_readiness(self, batch_id: int) -> dict:
        """冻结入口使用：展示当前指南版本下还缺哪些条目、哪些分歧未仲裁。"""
        batch = self.conn.execute(
            "SELECT b.*,g.version AS guideline_version FROM batches b "
            "JOIN guidelines g ON g.id=b.guideline_id WHERE b.id=?",
            (batch_id,),
        ).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        missing, pending = [], []
        for item in items:
            rows = self.conn.execute(
                "SELECT a.id,u.name AS annotator_name,a.label FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=?",
                (item["id"], batch["guideline_id"]),
            ).fetchall()
            if len(rows) < 2:
                missing.append({
                    "item_id": item["id"], "ordinal": item["ordinal"],
                    "annotation_count": len(rows),
                    "annotators": [r["annotator_name"] for r in rows],
                })
            elif self._open_disagreement(item["id"], batch["guideline_id"]):
                pending.append(item["id"])
        return {
            "batch_id": batch_id,
            "frozen": batch["status"] == "frozen",
            "guideline_id": batch["guideline_id"],
            "guideline_version": batch["guideline_version"],
            "item_count": len(items),
            "missing_items": missing,
            "unadjudicated_items": pending,
            "ready": not missing and not pending and bool(items) and batch["status"] != "frozen",
        }

    def _open_disagreement(self, item_id: int, guideline_id: int) -> bool:
        labels = [r["label"] for r in self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item_id, guideline_id)
        ).fetchall()]
        if len(labels) < 2 or len(set(labels)) <= 1:
            return False
        return self.conn.execute(
            "SELECT 1 FROM adjudications WHERE item_id=? AND guideline_id=?", (item_id, guideline_id)
        ).fetchone() is None

    def freeze_batch(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("批次已冻结，冻结后不能换版或修改")
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        disagreements = self.disagreements(batch_id)
        if disagreements:
            raise DomainError(f"当前指南版本仍有 {len(disagreements)} 条分歧未仲裁")
        missing = []
        for item in items:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchone()[0]
            if count < 2:
                missing.append({"item_id": item["id"], "ordinal": item["ordinal"], "annotation_count": count})
        if missing:
            raise DomainError(f"新版指南还没收齐，缺少标注的条目: {missing}")
        metrics = self.consistency(batch_id)
        frozen_at = datetime.now().isoformat()
        with self.transaction():
            for item in items:
                labels = [r["label"] for r in self.conn.execute(
                    "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
                ).fetchall()]
                adj = self.conn.execute(
                    "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                    (item["id"], batch["guideline_id"]),
                ).fetchone()
                if adj:
                    label, source, adj_id = adj["final_label"], "adjudication", adj["id"]
                else:
                    label, source, adj_id = labels[0], "consensus", None
                self.conn.execute(
                    "INSERT INTO gold_records(batch_id,item_id,guideline_id,label,source,adjudication_id,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["id"], batch["guideline_id"], label, source, adj_id, frozen_at),
                )
            self.conn.execute(
                "INSERT INTO batch_freezes(batch_id,metrics_json,frozen_by,frozen_at) VALUES(?,?,?,?)",
                (batch_id, json.dumps(metrics, ensure_ascii=False), manager_id, frozen_at),
            )
            self.conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (batch_id,))
        return {"batch_id": batch_id, "guideline_version": metrics["guideline_version"], "metrics": metrics, "frozen_at": frozen_at}

    def export_gold(self, batch_id: int) -> dict:
        batch = self.conn.execute(
            "SELECT b.*,g.version AS guideline_version FROM batches b JOIN guidelines g ON g.id=b.guideline_id WHERE b.id=?",
            (batch_id,),
        ).fetchone()
        if not batch or batch["status"] != "frozen":
            raise DomainError("只有已冻结批次可以导出金标准")
        freeze = self.conn.execute("SELECT * FROM batch_freezes WHERE batch_id=?", (batch_id,)).fetchone()
        rows = self.conn.execute(
            "SELECT g.item_id,i.ordinal,i.text,g.guideline_id,gv.version AS guideline_version,"
            "g.label,g.source,g.frozen_at "
            "FROM gold_records g JOIN items i ON i.id=g.item_id JOIN guidelines gv ON gv.id=g.guideline_id "
            "WHERE g.batch_id=? ORDER BY i.ordinal", (batch_id,)
        ).fetchall()
        return {
            "batch_id": batch_id, "batch_name": batch["name"],
            "guideline_id": batch["guideline_id"], "guideline_version": batch["guideline_version"],
            "frozen_at": freeze["frozen_at"],
            "metrics": json.loads(freeze["metrics_json"]),
            "records": [dict(row) for row in rows],
        }

    def snapshot(self) -> dict:
        batches = self.conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
        current = {row["id"]: row["guideline_id"] for row in batches}
        item_batch = {
            row["id"]: (row["batch_id"], current.get(row["batch_id"]))
            for row in self.conn.execute("SELECT id,batch_id FROM items").fetchall()
        }
        annotations = []
        for row in self.conn.execute(
            "SELECT an.*,u.name AS annotator_name,g.version AS guideline_version "
            "FROM annotations an JOIN users u ON u.id=an.annotator_id JOIN guidelines g ON g.id=an.guideline_id "
            "ORDER BY an.id"
        ).fetchall():
            rec = dict(row)
            rec["is_current"] = current.get(item_batch.get(rec["item_id"], (None, None))[0]) == rec["guideline_id"]
            annotations.append(rec)
        adjudications = [dict(row) for row in self.conn.execute(
            "SELECT ad.*,g.version AS guideline_version FROM adjudications ad "
            "JOIN guidelines g ON g.id=ad.guideline_id ORDER BY ad.id"
        ).fetchall()]
        switches = [dict(row) for row in self.conn.execute(
            "SELECT s.*,gf.version AS from_version,gt.version AS to_version,mu.name AS switched_by_name "
            "FROM guideline_switches s "
            "LEFT JOIN guidelines gf ON gf.id=s.from_guideline_id "
            "JOIN guidelines gt ON gt.id=s.to_guideline_id "
            "JOIN users mu ON mu.id=s.switched_by ORDER BY s.id"
        ).fetchall()]
        freezes = [dict(row, **{"metrics": json.loads(row["metrics_json"])})
                   for row in self.conn.execute("SELECT * FROM batch_freezes ORDER BY batch_id").fetchall()]
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": [dict(r) for r in batches],
            "items": [dict(r) for r in self.conn.execute("SELECT * FROM items ORDER BY batch_id,ordinal")],
            "assignments": [dict(r) for r in self.conn.execute("SELECT * FROM assignments ORDER BY id")],
            "annotations": annotations,
            "adjudications": adjudications,
            "guideline_switches": switches,
            "freezes": [{k: v for k, v in row.items() if k != "metrics_json"} for row in freezes],
        }

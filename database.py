from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime


class DomainError(ValueError):
    """Business rule violation."""


class CorpusDB:
    """A small multi-annotator corpus governance service."""

    REQUIRED_ANNOTATIONS = 2

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
            CREATE TABLE IF NOT EXISTS batch_guideline_switches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              from_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              to_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              manager_id INTEGER NOT NULL REFERENCES users(id),
              switched_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch_id);
            CREATE INDEX IF NOT EXISTS idx_assignments_batch ON assignments(batch_id);
            CREATE INDEX IF NOT EXISTS idx_annotations_item_guideline ON annotations(item_id, guideline_id);
            CREATE INDEX IF NOT EXISTS idx_adjudications_item_guideline ON adjudications(item_id, guideline_id);
            CREATE INDEX IF NOT EXISTS idx_guideline_switches_batch ON batch_guideline_switches(batch_id);
            """
        )
        self._migrate_adjudications()
        self.conn.commit()

    def _adjudication_unique_columns(self) -> tuple[str, ...] | None:
        for index in self.conn.execute("PRAGMA index_list('adjudications')").fetchall():
            if index["origin"] != "u":
                continue
            index_name = str(index["name"]).replace('"', '""')
            rows = self.conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            columns = tuple(row["name"] for row in rows)
            if columns:
                return columns
        return None

    def _migrate_adjudications(self) -> None:
        """Move databases created before item+guideline adjudication uniqueness."""
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adjudications'"
        ).fetchone():
            return
        if self._adjudication_unique_columns() == ("item_id", "guideline_id"):
            return

        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.transaction():
                self.conn.execute("ALTER TABLE adjudications RENAME TO adjudications_legacy")
                self.conn.execute(
                    """
                    CREATE TABLE adjudications (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                      guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
                      final_label TEXT NOT NULL,
                      reason TEXT NOT NULL,
                      arbitrator_id INTEGER NOT NULL REFERENCES users(id),
                      created_at TEXT NOT NULL,
                      UNIQUE(item_id, guideline_id)
                    )
                    """
                )
                self.conn.execute(
                    """
                    INSERT INTO adjudications(
                      id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at
                    )
                    SELECT id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at
                    FROM adjudications_legacy
                    """
                )
                self.conn.execute("DROP TABLE adjudications_legacy")
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_adjudications_item_guideline "
                    "ON adjudications(item_id, guideline_id)"
                )
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        self.add_user("仲裁员", "arbitrator")
        self.add_user("管理员", "manager")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        self.add_guideline("v2", "新版规则：标签仅可为 正向/负向/中性；反讽和弱情绪需在备注中说明。")
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
                self.conn.execute(
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

    def switch_batch_guideline(self, batch_id: int, guideline_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        target = self.conn.execute(
            "SELECT * FROM guidelines WHERE id=? AND active=1", (guideline_id,)
        ).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if not target:
            raise DomainError("新版指南不存在或未启用")
        if batch["status"] == "frozen":
            raise DomainError("冻结后不能换版")
        if batch["guideline_id"] == guideline_id:
            raise DomainError("批次已经绑定该指南版本")

        switched_at = datetime.now().isoformat()
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO batch_guideline_switches(
                  batch_id,from_guideline_id,to_guideline_id,manager_id,switched_at
                ) VALUES(?,?,?,?,?)
                """,
                (batch_id, batch["guideline_id"], guideline_id, manager_id, switched_at),
            )
            event_id = int(cur.lastrowid)
            self.conn.execute("UPDATE batches SET guideline_id=? WHERE id=?", (guideline_id, batch_id))
            self.conn.execute(
                """
                UPDATE assignments
                SET status = CASE
                  WHEN EXISTS (
                    SELECT 1 FROM annotations a
                    WHERE a.item_id=assignments.item_id
                      AND a.annotator_id=assignments.annotator_id
                      AND a.guideline_id=?
                  ) THEN 'submitted'
                  ELSE 'assigned'
                END
                WHERE batch_id=?
                """,
                (guideline_id, batch_id),
            )
        return self.guideline_switch(event_id)

    def guideline_switch(self, event_id: int) -> dict:
        row = self.conn.execute(
            """
            SELECT s.id,s.batch_id,s.from_guideline_id,s.to_guideline_id,s.manager_id,
                   u.name AS manager_name,s.switched_at,
                   gf.version AS from_version,gt.version AS to_version,
                   gt.rules AS to_rules
            FROM batch_guideline_switches s
            JOIN users u ON u.id=s.manager_id
            JOIN guidelines gf ON gf.id=s.from_guideline_id
            JOIN guidelines gt ON gt.id=s.to_guideline_id
            WHERE s.id=?
            """,
            (event_id,),
        ).fetchone()
        if not row:
            raise DomainError("换版记录不存在")
        return dict(row)

    def guideline_switches(self, batch_id: int) -> list[dict]:
        if not self.conn.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
            raise DomainError("批次不存在")
        return [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT s.id,s.batch_id,s.from_guideline_id,s.to_guideline_id,s.manager_id,
                       u.name AS manager_name,s.switched_at,
                       gf.version AS from_version,gt.version AS to_version,
                       gt.rules AS to_rules
                FROM batch_guideline_switches s
                JOIN users u ON u.id=s.manager_id
                JOIN guidelines gf ON gf.id=s.from_guideline_id
                JOIN guidelines gt ON gt.id=s.to_guideline_id
                WHERE s.batch_id=?
                ORDER BY s.id
                """,
                (batch_id,),
            ).fetchall()
        ]

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
            """
            SELECT i.id,i.batch_id,i.ordinal,i.text,g.id AS guideline_id,
                   g.version AS guideline_version,g.rules
            FROM items i
            JOIN batches b ON b.id=i.batch_id
            JOIN guidelines g ON g.id=b.guideline_id
            WHERE i.id=?
            """,
            (item_id,),
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        own = self.conn.execute(
            """
            SELECT a.id,a.guideline_id,g.version AS guideline_version,a.label,a.comment,a.created_at
            FROM annotations a JOIN guidelines g ON g.id=a.guideline_id
            WHERE a.item_id=? AND a.annotator_id=? AND a.guideline_id=?
            ORDER BY a.id
            """,
            (item_id, user_id, item["guideline_id"]),
        ).fetchone()
        own_annotations = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT a.id,a.guideline_id,g.version AS guideline_version,a.label,a.comment,a.created_at
                FROM annotations a JOIN guidelines g ON g.id=a.guideline_id
                WHERE a.item_id=? AND a.annotator_id=?
                ORDER BY a.guideline_id,a.id
                """,
                (item_id, user_id),
            ).fetchall()
        ]
        revealed = own is not None
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id", (item_id,)
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的当前版本标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload = dict(item)
        payload["own_annotation"] = dict(own) if own else None
        payload["own_annotations"] = own_annotations
        payload["discussions"] = discussions
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            rows = self.conn.execute(
                """
                SELECT a.*,u.name,g.version AS guideline_version
                FROM annotations a
                JOIN users u ON u.id=a.annotator_id
                JOIN guidelines g ON g.id=a.guideline_id
                WHERE a.item_id=? AND a.guideline_id=?
                ORDER BY a.id
                """,
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
            raise DomainError("当前指南至少需要两份标注才能仲裁")
        if not final_label.strip() or len(reason.strip()) < 5:
            raise DomainError("最终标签必填，仲裁理由至少5个字符")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjudications(item_id,guideline_id,final_label,reason,arbitrator_id,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, item["guideline_id"], final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                self.conn.execute(
                    "UPDATE adjudications SET final_label=?,reason=?,arbitrator_id=?,created_at=? WHERE item_id=? AND guideline_id=?",
                    (final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat(), item_id, item["guideline_id"]),
                )
                adjudication_id = self.conn.execute(
                    "SELECT id FROM adjudications WHERE item_id=? AND guideline_id=?",
                    (item_id, item["guideline_id"]),
                ).fetchone()["id"]
            else:
                adjudication_id = int(cur.lastrowid)
        return int(adjudication_id)

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute(
            """
            SELECT b.*,g.version AS guideline_version
            FROM batches b JOIN guidelines g ON g.id=b.guideline_id
            WHERE b.id=?
            """,
            (batch_id,),
        ).fetchone()
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
            "guideline_version": batch["guideline_version"],
            "items_with_multiple_annotations": total_pairs and sum(1 for row in per_item if row["agreement"] is not None),
            "pairwise_agreement": round(pairwise, 4) if pairwise is not None else None,
            "fleiss_kappa": round(kappa, 4) if kappa is not None else None,
            "items": per_item,
        }

    def freeze_status(self, batch_id: int) -> dict:
        batch = self.conn.execute(
            """
            SELECT b.*,g.version AS guideline_version,g.rules AS guideline_rules
            FROM batches b JOIN guidelines g ON g.id=b.guideline_id
            WHERE b.id=?
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise DomainError("批次不存在")

        missing = []
        items = self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        for item in items:
            assigned_rows = self.conn.execute(
                "SELECT annotator_id FROM assignments WHERE item_id=? ORDER BY annotator_id", (item["id"],)
            ).fetchall()
            assigned_annotators = [row["annotator_id"] for row in assigned_rows]
            current_rows = self.conn.execute(
                "SELECT annotator_id FROM annotations WHERE item_id=? AND guideline_id=?",
                (item["id"], batch["guideline_id"]),
            ).fetchall()
            current_annotators = [row["annotator_id"] for row in current_rows]
            current_set = set(current_annotators)
            missing_annotators = [user_id for user_id in assigned_annotators if user_id not in current_set]
            required = max(self.REQUIRED_ANNOTATIONS, len(assigned_annotators))
            if len(current_rows) < self.REQUIRED_ANNOTATIONS or missing_annotators:
                missing.append({
                    "item_id": item["id"],
                    "ordinal": item["ordinal"],
                    "text": item["text"],
                    "current_annotations": len(current_rows),
                    "required_annotations": required,
                    "current_annotator_ids": current_annotators,
                    "missing_annotator_ids": missing_annotators,
                })

        unresolved = self.disagreements(batch_id)
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "current_guideline": {
                "id": batch["guideline_id"],
                "version": batch["guideline_version"],
                "rules": batch["guideline_rules"],
            },
            "can_switch": batch["status"] != "frozen",
            "can_freeze": batch["status"] != "frozen" and bool(items) and not missing and not unresolved,
            "ready_to_freeze": batch["status"] != "frozen" and bool(items) and not missing and not unresolved,
            "missing_item_ids": [row["item_id"] for row in missing],
            "missing_annotations": missing,
            "unresolved_disagreements": unresolved,
        }

    def freeze_batch(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("批次已冻结")
        items = self.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        status = self.freeze_status(batch_id)
        if status["unresolved_disagreements"]:
            raise DomainError(f"仍有 {len(status['unresolved_disagreements'])} 条当前版本分歧未仲裁")
        if status["missing_annotations"]:
            raise DomainError(f"当前指南标注未收齐，缺少条目: {status['missing_item_ids']}")
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
        return {
            "batch_id": batch_id,
            "guideline_id": batch["guideline_id"],
            "metrics": metrics,
            "frozen_at": frozen_at,
        }

    def export_gold(self, batch_id: int) -> dict:
        batch = self.conn.execute(
            """
            SELECT b.*,g.version AS guideline_version
            FROM batches b JOIN guidelines g ON g.id=b.guideline_id
            WHERE b.id=?
            """,
            (batch_id,),
        ).fetchone()
        if not batch or batch["status"] != "frozen":
            raise DomainError("只有已冻结批次可以导出金标准")
        freeze = self.conn.execute("SELECT * FROM batch_freezes WHERE batch_id=?", (batch_id,)).fetchone()
        rows = self.conn.execute(
            """
            SELECT g.item_id,i.ordinal,i.text,g.guideline_id,gg.version AS guideline_version,
                   g.label,g.source,g.frozen_at
            FROM gold_records g
            JOIN items i ON i.id=g.item_id
            JOIN guidelines gg ON gg.id=g.guideline_id
            WHERE g.batch_id=?
            ORDER BY i.ordinal
            """,
            (batch_id,),
        ).fetchall()
        return {
            "batch_id": batch_id,
            "batch_name": batch["name"],
            "guideline_id": batch["guideline_id"],
            "guideline_version": batch["guideline_version"],
            "frozen_at": freeze["frozen_at"],
            "metrics": json.loads(freeze["metrics_json"]),
            "records": [dict(row) for row in rows],
        }

    def snapshot(self) -> dict:
        annotations = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT a.*,u.name AS annotator_name,g.version AS guideline_version
                FROM annotations a
                JOIN users u ON u.id=a.annotator_id
                JOIN guidelines g ON g.id=a.guideline_id
                ORDER BY a.id
                """
            ).fetchall()
        ]
        adjudications = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT ad.*,u.name AS arbitrator_name,g.version AS guideline_version
                FROM adjudications ad
                JOIN users u ON u.id=ad.arbitrator_id
                JOIN guidelines g ON g.id=ad.guideline_id
                ORDER BY ad.id
                """
            ).fetchall()
        ]
        freezes = []
        for row in self.conn.execute("SELECT * FROM batch_freezes ORDER BY batch_id").fetchall():
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            freezes.append(item)
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY id")],
            "items": [dict(r) for r in self.conn.execute("SELECT * FROM items ORDER BY batch_id,ordinal")],
            "assignments": [dict(r) for r in self.conn.execute("SELECT * FROM assignments ORDER BY id")],
            "annotations": annotations,
            "adjudications": adjudications,
            "guideline_switches": [
                dict(row)
                for row in self.conn.execute(
                    """
                    SELECT s.*,gf.version AS from_version,gt.version AS to_version,u.name AS manager_name
                    FROM batch_guideline_switches s
                    JOIN guidelines gf ON gf.id=s.from_guideline_id
                    JOIN guidelines gt ON gt.id=s.to_guideline_id
                    JOIN users u ON u.id=s.manager_id
                    ORDER BY s.id
                    """
                ).fetchall()
            ],
            "freezes": freezes,
            "gold_records": [
                dict(row)
                for row in self.conn.execute(
                    """
                    SELECT gr.*,g.version AS guideline_version
                    FROM gold_records gr JOIN guidelines g ON g.id=gr.guideline_id
                    ORDER BY gr.id
                    """
                ).fetchall()
            ],
        }

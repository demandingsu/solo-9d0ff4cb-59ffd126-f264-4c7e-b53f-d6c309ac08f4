"""同景认定与影像组管理核心逻辑。

领域规则（需求原文）：
- 按两条场景各自的批次号 + 来源行号定位场景，登记（assert）或撤销（revoke）同景认定。
- 只可认定同一 UTC 观测日的两条场景。
- 同景认定可传递：认定关系是无向边，连通分量即同一个影像组。
- 参与过认定的场景若已不和任何场景相连，就单独成组。
- 分量成员发生变化时发放新组号；成员没变（重复登记、撤销后仍相连）则沿用现号。
- 数仓按旧组号回溯时，每个仍含有原成员的现组列一次，并列出落在其中的原成员。

实现要点：
- 认定关系以 append-only 事件流保存（same_scene_assertions），
  每对场景最后一条事件决定该边当前是否有效，撤销不物理删除，历史可追溯。
- 每次发号写不可变快照（scene_groups / scene_group_members），
  当前归属用 scene_group_current 维护；事件、快照、当前归属在同一事务内更新，
  任一步失败整体回滚，失败时查询仍返回改动前的结果。
"""
from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


class GroupingError(Exception):
    """可转为 HTTP 错误的业务异常。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------- 场景定位 ----------------


def _resolve_scene(conn: sqlite3.Connection, batch_id: int, source_line: int,
                   side: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, batch_id, scene_no, observation_date, source_line "
        "FROM scenes WHERE batch_id=? AND source_line=?",
        (batch_id, source_line),
    ).fetchone()
    if row is None:
        raise GroupingError(
            404,
            f"{side}场景不存在: batch_id={batch_id}, source_line={source_line}",
        )
    return row


def _scene_ref(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "batch_id": row["batch_id"],
        "source_line": row["source_line"],
        "scene_no": row["scene_no"],
        "observation_date": row["observation_date"],
        "scene_id": row["id"],
    }


# ---------------- 图计算 ----------------


def _load_active_edges(conn: sqlite3.Connection) -> Dict[FrozenSet[int], sqlite3.Row]:
    """读取当前有效认定边（每对场景取最后一条事件，action=assert 才有效）。"""
    rows = conn.execute(
        "SELECT ssa.* FROM same_scene_assertions ssa "
        "JOIN (SELECT scene_a_id, scene_b_id, MAX(id) AS max_id "
        "      FROM same_scene_assertions GROUP BY scene_a_id, scene_b_id) latest "
        "  ON latest.max_id = ssa.id "
        "WHERE ssa.action='assert'"
    ).fetchall()
    return {frozenset((r["scene_a_id"], r["scene_b_id"])): r for r in rows}


def _components(edges: Dict[FrozenSet[int], sqlite3.Row],
                seeds: Optional[List[int]] = None
                ) -> List[FrozenSet[int]]:
    """基于有效边求连通分量；给定 seeds 时只返回与 seeds 相关的分量。"""
    adj: Dict[int, set] = {}
    for edge in edges:
        a, b = tuple(edge)
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    components: List[FrozenSet[int]] = []
    if seeds is None:
        seen: set = set()
        for node in sorted(adj):
            if node in seen:
                continue
            comp = _bfs(adj, node)
            seen |= comp
            components.append(frozenset(comp))
        return components

    visited: set = set()
    for seed in seeds:
        if seed in visited:
            continue
        comp = _bfs(adj, seed)
        visited |= comp
        components.append(frozenset(comp))
    return components


def _bfs(adj: Dict[int, set], start: int) -> set:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in adj.get(node, ()):  # 无邻居时 start 自身成为单点分量
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


# ---------------- 组号 ----------------


def _next_group_no(conn: sqlite3.Connection) -> str:
    """在事务内取下一个组号（G1、G2 …）；组号只增不复用。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM scene_groups"
    ).fetchone()
    return f"G{row['next_id']}"


def _current_groups_of(conn: sqlite3.Connection,
                       components: List[FrozenSet[int]]) -> Dict[FrozenSet[int], Optional[str]]:
    """查每个分量当前对应的组号：分量所有成员当前归属同一组才返回该组号。"""
    result: Dict[FrozenSet[int], Optional[str]] = {}
    for comp in components:
        members = sorted(comp)
        placeholders = ",".join("?" for _ in members)
        rows = conn.execute(
            f"SELECT group_no FROM scene_group_current WHERE scene_id IN ({placeholders})",
            members,
        ).fetchall()
        group_nos = {r["group_no"] for r in rows}
        result[comp] = next(iter(group_nos)) if len(group_nos) == 1 else None
    return result


def _issue_group(conn: sqlite3.Connection, group_no: str,
                 component: FrozenSet[int], assertion_id: int,
                 action: str) -> None:
    """发放新组号：写不可变快照，并整体替换这些成员的当前归属。"""
    conn.execute(
        "INSERT INTO scene_groups (group_no, source_assertion_id, change_action) "
        "VALUES (?, ?, ?)",
        (group_no, assertion_id, action),
    )
    conn.executemany(
        "INSERT INTO scene_group_members (group_no, scene_id) VALUES (?, ?)",
        [(group_no, sid) for sid in sorted(component)],
    )
    conn.executemany(
        "DELETE FROM scene_group_current WHERE scene_id=?",
        [(sid,) for sid in sorted(component)],
    )
    conn.executemany(
        "INSERT INTO scene_group_current (scene_id, group_no) VALUES (?, ?)",
        [(sid, group_no) for sid in sorted(component)],
    )


# ---------------- 认定登记 / 撤销 ----------------


def apply_assertion(conn: sqlite3.Connection,
                    action: str,
                    first: Tuple[int, int],
                    second: Tuple[int, int]) -> Dict[str, Any]:
    """登记或撤销一条同景认定，事务内完成关系变更与组号重算。

    first/second 为 (batch_id, source_line)。返回涉及场景当前所在组。
    """
    row_a = _resolve_scene(conn, first[0], first[1], "first")
    row_b = _resolve_scene(conn, second[0], second[1], "second")
    if row_a["id"] == row_b["id"]:
        raise GroupingError(422, "两条场景指向同一个场景，不能对自身登记同景认定")
    if row_a["observation_date"] != row_b["observation_date"]:
        raise GroupingError(
            409,
            f"只可认定同一 UTC 观测日的场景: "
            f"{row_a['scene_no']}@{row_a['observation_date']} vs "
            f"{row_b['scene_no']}@{row_b['observation_date']}",
        )

    a_id, b_id = sorted((row_a["id"], row_b["id"]))
    edge_key = frozenset((a_id, b_id))

    # 变更前的有效边与相关连通分量（含两个端点；孤立点也会得到单点分量）
    before_edges = _load_active_edges(conn)
    before_components = _components(before_edges, seeds=[a_id, b_id])
    before_map: Dict[FrozenSet[int], FrozenSet[int]] = {}
    for comp in before_components:
        for sid in comp:
            before_map[sid] = comp

    existing = before_edges.get(edge_key)
    if action == "assert" and existing is not None:
        raise GroupingError(409, "这两条场景已存在有效的同景认定，无需重复登记")
    if action == "revoke" and existing is None:
        raise GroupingError(404, "这两条场景当前没有有效的同景认定可撤销")

    cur = conn.execute(
        "INSERT INTO same_scene_assertions (scene_a_id, scene_b_id, action) "
        "VALUES (?, ?, ?)",
        (a_id, b_id, action),
    )
    assertion_id = cur.lastrowid

    # 变更后的有效边与相关分量
    if action == "assert":
        after_edges = dict(before_edges)
        after_edges[edge_key] = assertion_id  # 值仅占位，分量计算只用到边的 key
    else:
        after_edges = dict(before_edges)
        after_edges.pop(edge_key, None)
    after_components = _components(after_edges, seeds=[a_id, b_id])

    # 发号规则：新分量与变更前某分量成员完全相同 -> 沿用现号；否则发新号
    current_lookup = _current_groups_of(conn, before_components)
    affected_groups: Dict[str, Dict[str, Any]] = {}
    for comp in after_components:
        old_comp = before_map.get(next(iter(comp)))
        reused = old_comp is not None and old_comp == comp
        if reused:
            group_no = current_lookup[old_comp]
            # 成员集合完全相同意味着该分量在变更前就已同组，组号必然存在
            if group_no is None:  # 防御性兜底，理论不可达
                reused = False
        if not reused:
            group_no = _next_group_no(conn)
            _issue_group(conn, group_no, comp, assertion_id, action)
        affected_groups[group_no] = {
            "group_no": group_no,
            "reused": reused,
            "scene_ids": sorted(comp),
        }

    return {
        "action": action,
        "assertion_id": assertion_id,
        "first_scene": _scene_ref(row_a),
        "second_scene": _scene_ref(row_b),
        "group_nos": sorted(affected_groups),
        "groups": list(affected_groups.values()),
    }


# ---------------- 查询 ----------------


def _hydrate_group(conn: sqlite3.Connection, group_no: str,
                   member_filter: Optional[List[int]] = None) -> Dict[str, Any]:
    """组装一个当前组的明细；member_filter 给定时只列出落在其中的指定原成员。"""
    if member_filter is None:
        rows = conn.execute(
            "SELECT sc.id AS scene_id, sc.batch_id, sc.scene_no, sc.observation_date, "
            "sc.source_line FROM scene_group_current sgc "
            "JOIN scenes sc ON sc.id = sgc.scene_id "
            "WHERE sgc.group_no=? ORDER BY sc.batch_id, sc.source_line",
            (group_no,),
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in member_filter)
        rows = conn.execute(
            f"SELECT sc.id AS scene_id, sc.batch_id, sc.scene_no, sc.observation_date, "
            f"sc.source_line FROM scene_group_current sgc "
            f"JOIN scenes sc ON sc.id = sgc.scene_id "
            f"WHERE sgc.group_no=? AND sc.id IN ({placeholders}) "
            f"ORDER BY sc.batch_id, sc.source_line",
            [group_no, *member_filter],
        ).fetchall()
    return {
        "group_no": group_no,
        "members": [
            {
                "scene_id": r["scene_id"],
                "batch_id": r["batch_id"],
                "source_line": r["source_line"],
                "scene_no": r["scene_no"],
                "observation_date": r["observation_date"],
            }
            for r in rows
        ],
    }


def list_current_groups(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """列出所有当前影像组（参与过认定的场景才在组内）。"""
    rows = conn.execute(
        "SELECT DISTINCT group_no FROM scene_group_current ORDER BY group_no"
    ).fetchall()
    return [_hydrate_group(conn, r["group_no"]) for r in rows]


def get_current_group(conn: sqlite3.Connection, group_no: str) -> Dict[str, Any]:
    if not conn.execute(
        "SELECT 1 FROM scene_group_current WHERE group_no=? LIMIT 1", (group_no,)
    ).fetchone():
        raise GroupingError(404, f"当前不存在该影像组: group_no={group_no}")
    return _hydrate_group(conn, group_no)


def trace_old_group(conn: sqlite3.Connection, old_group_no: str) -> Dict[str, Any]:
    """按旧组号回溯：返回快照原成员，以及每个仍含有原成员的现组（各列一次）。"""
    snapshot = conn.execute(
        "SELECT id, created_at FROM scene_groups WHERE group_no=?", (old_group_no,)
    ).fetchone()
    if snapshot is None:
        raise GroupingError(404, f"旧组号不存在: group_no={old_group_no}")

    member_rows = conn.execute(
        "SELECT sc.id AS scene_id, sc.batch_id, sc.scene_no, sc.observation_date, "
        "sc.source_line FROM scene_group_members gm "
        "JOIN scenes sc ON sc.id = gm.scene_id "
        "WHERE gm.group_no=? ORDER BY sc.batch_id, sc.source_line",
        (old_group_no,),
    ).fetchall()
    original_members = [
        {
            "scene_id": r["scene_id"],
            "batch_id": r["batch_id"],
            "source_line": r["source_line"],
            "scene_no": r["scene_no"],
            "observation_date": r["observation_date"],
        }
        for r in member_rows
    ]
    original_ids = [r["scene_id"] for r in member_rows]
    if not original_ids:
        return {"old_group_no": old_group_no, "original_members": [], "current_groups": []}

    placeholders = ",".join("?" for _ in original_ids)
    current_rows = conn.execute(
        f"SELECT DISTINCT group_no FROM scene_group_current "
        f"WHERE scene_id IN ({placeholders}) ORDER BY group_no",
        original_ids,
    ).fetchall()
    current_groups = [
        _hydrate_group(conn, r["group_no"], member_filter=original_ids)
        for r in current_rows
    ]
    return {
        "old_group_no": old_group_no,
        "original_members": original_members,
        "current_groups": current_groups,
    }


def list_assertion_events(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """列出同景认定事件流（append-only，含已撤销事件），便于从 /docs 核查。"""
    rows = conn.execute(
        "SELECT ssa.id, ssa.action, ssa.created_at, "
        "sa.batch_id AS batch_a, sa.source_line AS line_a, sa.scene_no AS scene_a, "
        "sa.observation_date AS date_a, "
        "sb.batch_id AS batch_b, sb.source_line AS line_b, sb.scene_no AS scene_b, "
        "sb.observation_date AS date_b "
        "FROM same_scene_assertions ssa "
        "JOIN scenes sa ON sa.id = ssa.scene_a_id "
        "JOIN scenes sb ON sb.id = ssa.scene_b_id "
        "ORDER BY ssa.id"
    ).fetchall()
    return [
        {
            "assertion_id": r["id"],
            "action": r["action"],
            "created_at": r["created_at"],
            "first_scene": {
                "batch_id": r["batch_a"], "source_line": r["line_a"],
                "scene_no": r["scene_a"], "observation_date": r["date_a"],
            },
            "second_scene": {
                "batch_id": r["batch_b"], "source_line": r["line_b"],
                "scene_no": r["scene_b"], "observation_date": r["date_b"],
            },
        }
        for r in rows
    ]

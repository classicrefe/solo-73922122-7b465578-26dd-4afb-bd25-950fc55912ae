"""规划器单元测试：python -m unittest discover tests -v"""
import unittest

from app.planner import (build_plan, parse_finished_at, partition_matches,
                         shift_partitions)


def T(tid, name, gran="daily", ver=1):
    return {"id": tid, "name": name, "granularity": gran, "config_version": ver}


def D(did, down, up, off=0):
    return {"id": did, "downstream_id": down, "upstream_id": up, "offset_days": off}


def R(rid, tid, part, status="success", ver=1, ts="2026-11-14T01:00:00"):
    return {"id": rid, "task_id": tid, "partition": part, "status": status,
            "config_version": ver, "finished_at": ts}


class TestShift(unittest.TestCase):
    def test_daily_offset(self):
        self.assertEqual(shift_partitions("daily", "daily", "2026-11-13", 1),
                         ["2026-11-12"])

    def test_hourly_same_hour(self):
        self.assertEqual(shift_partitions("hourly", "hourly", "2026-11-13T08", 1),
                         ["2026-11-12T08"])

    def test_daily_down_hourly_up_expands_24(self):
        got = shift_partitions("hourly", "daily", "2026-11-13", 0)
        self.assertEqual(len(got), 24)
        self.assertEqual(got[0], "2026-11-13T00")
        self.assertEqual(got[-1], "2026-11-13T23")

    def test_matches(self):
        self.assertTrue(partition_matches("daily", "2026-11-13"))
        self.assertTrue(partition_matches("hourly", "2026-11-13T08"))
        self.assertFalse(partition_matches("daily", "2026-11-13T08"))
        self.assertFalse(partition_matches("hourly", "2026-13-01"))


class TestParseFinishedAt(unittest.TestCase):
    def test_naive(self):
        kind, dt = parse_finished_at("2026-11-14T01:00:00")
        self.assertEqual(kind, "naive")
        self.assertEqual(dt.isoformat(), "2026-11-14T01:00:00")

    def test_aware_offset(self):
        kind, dt = parse_finished_at("2026-11-14T01:00:00+08:00")
        self.assertEqual(kind, "aware")
        self.assertEqual(dt.utcoffset().total_seconds(), 8 * 3600)

    def test_aware_z(self):
        kind, dt = parse_finished_at("2026-11-14T01:00:00Z")
        self.assertEqual(kind, "aware")
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_minutes_and_fraction_ok(self):
        self.assertIsNotNone(parse_finished_at("2026-11-14T01:02"))
        self.assertIsNotNone(parse_finished_at("2026-11-14T01:02:03.123456"))

    def test_invalid(self):
        for bad in ("2026-11-14", "2026-11-14 01:00:00", "2026-13-14T01:00:00",
                    "2026-11-14T25:00:00", "2026-11-14T01:00:00+0800",
                    "昨天下午", "", None, 123):
            self.assertIsNone(parse_finished_at(bad), repr(bad))


class TestLatestByFinishedAt(unittest.TestCase):
    """同任务同分区的最新记录判定：时区感知排序 + 混用/坏时间拒绝。"""

    def setUp(self):
        # a(daily) <- b(daily, off1)：a@11-13 依赖 b@11-12
        self.tasks = [T(1, "a"), T(2, "b")]
        self.deps = [D(1, 1, 2, 1)]

    def plan(self, records, deps=None):
        return build_plan(self.tasks, deps or self.deps, records, 1,
                          "2026-11-13", now="2026-11-14T09:00:00")

    def status_of_b(self, p, part="2026-11-12"):
        return next(i for i in p["items"]
                    if i["key"] == f"b:{part}")["status"]

    def test_aware_compared_by_absolute_instant(self):
        # 字符串上 success 更晚，但实际时刻 failed 更晚 -> failed 才是最新
        recs = [
            R(1, 2, "2026-11-12", status="success",
              ts="2026-11-14T10:00:00+08:00"),  # = 2026-11-14T02:00:00Z
            R(2, 2, "2026-11-12", status="failed",
              ts="2026-11-14T03:00:00+00:00"),  # = 2026-11-14T03:00:00Z
        ]
        p = self.plan(recs)
        self.assertEqual(p["status"], "generated")
        self.assertEqual(self.status_of_b(p), "failed")

    def test_aware_same_instant_higher_id_wins(self):
        # 不同时区表示的同一时刻 -> 记录号大者为最新
        recs = [
            R(1, 2, "2026-11-12", status="success",
              ts="2026-11-14T10:00:00+08:00"),  # = 2026-11-14T02:00:00Z
            R(2, 2, "2026-11-12", status="failed",
              ts="2026-11-14T02:00:00Z"),       # 同一时刻，id 更大
        ]
        p = self.plan(recs)
        self.assertEqual(self.status_of_b(p), "failed")

    def test_naive_keeps_legacy_order(self):
        # 无时区时间之间沿用原有（字符串）顺序
        recs = [
            R(1, 2, "2026-11-12", status="failed", ts="2026-11-14T01:00:00"),
            R(2, 2, "2026-11-12", status="success", ts="2026-11-14T02:00:00"),
        ]
        p = self.plan(recs)
        self.assertEqual(self.status_of_b(p), "ok")

    def test_mixed_kinds_rejected_with_partition(self):
        recs = [
            R(1, 2, "2026-11-12", status="success", ts="2026-11-14T01:00:00"),
            R(2, 2, "2026-11-12", status="failed",
              ts="2026-11-14T02:00:00+08:00"),
        ]
        p = self.plan(recs)
        self.assertEqual(p["status"], "rejected")
        self.assertIn("混用", p["reject_reason"])
        self.assertIn("2026-11-12", p["reject_reason"])  # 指出分区

    def test_unparseable_rejected_with_partition(self):
        recs = [R(1, 2, "2026-11-12", ts="昨天下午")]
        p = self.plan(recs)
        self.assertEqual(p["status"], "rejected")
        self.assertIn("无法解析", p["reject_reason"])
        self.assertIn("2026-11-12", p["reject_reason"])  # 指出分区

    def test_different_partitions_may_use_different_kinds(self):
        # 混用判定以 任务+分区 为界：不同分区各自用一类时间，不拒绝
        deps = [D(1, 1, 2, 0)]  # a@11-13 依赖 b@11-13
        recs = [
            R(1, 2, "2026-11-13", status="success", ts="2026-11-14T01:00:00"),
            R(2, 2, "2026-11-12", status="success",
              ts="2026-11-14T01:00:00+08:00"),
        ]
        p = self.plan(recs, deps=deps)
        self.assertEqual(p["status"], "generated")
        self.assertEqual(self.status_of_b(p, "2026-11-13"), "ok")

    def test_unreachable_bad_time_ignored(self):
        # 不可达任务的坏时间记录不参与判定
        self.tasks.append(T(9, "unrelated"))
        recs = [R(1, 9, "2026-11-12", ts="垃圾时间")]
        p = self.plan(recs)
        self.assertEqual(p["status"], "generated")


class TestBuildPlan(unittest.TestCase):
    def setUp(self):
        # a(daily) <- b(daily, off1) <- c(hourly, off0)
        self.tasks = [T(1, "a"), T(2, "b"), T(3, "c", "hourly")]
        self.deps = [D(1, 1, 2, 1), D(2, 2, 3, 0)]

    def plan(self, records, target=1, biz="2026-11-13"):
        return build_plan(self.tasks, self.deps, records, target, biz,
                          now="2026-11-14T09:00:00")

    def test_all_missing_backfill_waves(self):
        p = self.plan([])
        self.assertEqual(p["status"], "generated")
        by_key = {i["key"]: i for i in p["items"]}
        # a@11-13 -> b@11-12 -> c@11-12 全部 24 小时
        self.assertEqual(by_key["a:2026-11-13"]["wave"], 3)
        self.assertEqual(by_key["b:2026-11-12"]["wave"], 2)
        for h in range(24):
            self.assertEqual(by_key[f"c:2026-11-12T{h:02d}"]["wave"], 1)
        self.assertEqual(p["summary"]["waves"], 3)
        self.assertEqual(p["summary"]["backfill"], 26)

    def test_ok_prunes_upstream(self):
        # b@11-12 已成功且版本当前 -> 不再展开 c
        recs = [R(1, 2, "2026-11-12")]
        p = self.plan(recs)
        keys = {i["key"] for i in p["items"]}
        self.assertIn("b:2026-11-12", keys)
        self.assertFalse(any(k.startswith("c:") for k in keys))
        b = next(i for i in p["items"] if i["key"] == "b:2026-11-12")
        self.assertEqual(b["action"], "skip")
        a = next(i for i in p["items"] if i["key"] == "a:2026-11-13")
        self.assertEqual(a["wave"], 1)  # 唯一待回填节点

    def test_failed_and_stale_and_missing(self):
        recs = [
            R(1, 2, "2026-11-12", status="failed"),          # 失败
            R(2, 3, "2026-11-12T00", ver=1),                  # 版本过期的前提：c 升到 v2
            R(3, 3, "2026-11-12T01", ver=2),                  # 当前版本成功
        ]
        self.tasks[2]["config_version"] = 2
        p = self.plan(recs)
        by_key = {i["key"]: i for i in p["items"]}
        self.assertEqual(by_key["b:2026-11-12"]["status"], "failed")
        self.assertEqual(by_key["c:2026-11-12T00"]["status"], "stale")
        self.assertEqual(by_key["c:2026-11-12T01"]["status"], "ok")
        self.assertEqual(by_key["c:2026-11-12T02"]["status"], "missing")

    def test_latest_record_wins(self):
        recs = [R(1, 2, "2026-11-12", status="failed", ts="2026-11-14T01:00:00"),
                R(2, 2, "2026-11-12", status="success", ts="2026-11-14T02:00:00")]
        p = self.plan(recs)
        b = next(i for i in p["items"] if i["key"] == "b:2026-11-12")
        self.assertEqual(b["status"], "ok")

    def test_cycle_rejected(self):
        self.deps.append(D(3, 3, 1, 1))  # c -> a 成环
        p = self.plan([])
        self.assertEqual(p["status"], "rejected")
        self.assertIn("成环", p["reject_reason"])

    def test_inconsistent_record_rejected(self):
        # 记录版本高于当前配置版本
        recs = [R(1, 2, "2026-11-12", ver=9)]
        p = self.plan(recs)
        self.assertEqual(p["status"], "rejected")
        self.assertIn("不一致", p["reject_reason"])

    def test_granularity_mismatch_rejected(self):
        recs = [R(1, 3, "2026-11-12")]  # c 是小时任务，却录了日分区
        p = self.plan(recs)
        self.assertEqual(p["status"], "rejected")

    def test_irrelevant_record_ignored(self):
        # 不可达任务的脏记录不影响生成
        self.tasks.append(T(9, "unrelated"))
        recs = [R(1, 9, "bad-partition", ver=99)]
        p = self.plan(recs)
        self.assertEqual(p["status"], "generated")

    def test_hourly_target_expands_24(self):
        p = self.plan([], target=3, biz="2026-11-13")
        self.assertEqual(p["status"], "generated")
        self.assertEqual(len(p["items"]), 24)

    def test_bad_biz_date_rejected(self):
        p = self.plan([], biz="2026-13-40")
        self.assertEqual(p["status"], "rejected")

    def test_snapshot_contains_config(self):
        p = self.plan([])
        names = {t["name"] for t in p["snapshot"]["tasks"]}
        self.assertEqual(names, {"a", "b", "c"})
        self.assertEqual(len(p["snapshot"]["dependencies"]), 2)


if __name__ == "__main__":
    unittest.main()

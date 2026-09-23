# 课表、DDL、校园事务与双校区

固定活动需要绝对日期、起止时刻、校区和地点。课程另需学期第 1 周周一、星期、明确周次及例外日期。钟点不能从旧作息表猜测。

2026—2027 校历秋季第 1 周从 **2026-09-14** 开始；新生 09-21 上课是学校第 2 周。用 `calendar-2026-2027` 回读来源，不能以后每学期沿用。

DDL 没有物理地点、不占用一天。写作另列工作时间块。通知只写“10 月 8 日前”就保存原文和日期精度，不能添加 23:59；提前提醒时间由用户选择。

## 输入与运行

运行 `python3 scripts/planner.py <包外输入.json>`，结构见 [演示日程](../examples/plan-demo.json)。它只计算，不写入个人日历。

- `events`：`id/title/start/end`；可加 `location/campus/mode`（`physical`／`online`）。
- `deadlines`：`id/title/due`；精确截止用带时区 ISO 时间，只有日期加 `precision: date`。不附 `start/end/location`。
- `courses`：`id/title/weekday/weeks/start_time/end_time` 及地点校区。`semester.week1_monday` 指定锚点；`exclude_dates` 排除已确认停课日。调课用单次事件补入。
- `routes`：`from/to/from_campus/to_campus/minutes/source`，可加 `valid_on`。用户估计必须标为估计。班车衔接还须考虑步行、候车和到场余量。
- `arrival_buffer_minutes`：用户选择的到场缓冲，不是学校标准。
- `flexible`：`id/title/duration_minutes/location/campus/windows`；窗口须是真实可办理时段，不假设办公室全天营业。

需要保存时使用包外路径：

    python3 scripts/planner.py <输入.json> --output <包外目录>/计划.json --ics <包外目录>/计划.ics

输出存在时不会覆盖；确实更新同一草稿才加 `--overwrite`，由宿主先保留旧版。稳定 ID 应沿用，不用标题作唯一键。

## 解释结果

| 状态 | 应如何说 |
|---|---|
| `conflicts_found` | 哪两项重叠／衔接不足，影响与调整选项 |
| `travel_unverified` | 时刻无重叠，但交通或地点缺失，尚不能确认赶得上 |
| `consistent_with_supplied_constraints` | 按输入数据可衔接，不保证真实校园数据完整 |
| `no_feasible_slot` | 给定窗口排不下，考虑改日期、时长或已有安排 |
| `proposals_only` | 最多三个互斥备选；选中后才写入固定安排 |

工具检查所有事件对，长活动包住多个短活动不会漏检。两校区同名教室不视为同一地点。未知路线不填默认车程；仅凭“10:00 海棠下课、10:30 苏稽上课”不能宣布来得及。

弹性任务按五分钟网格查找，不是全局最优求解。多项分别推荐，用户选定后合并重跑，检测彼此冲突。

## 交付

先按时间列活动，再单列 DDL、冲突／交通未知点和调整选项。地图距离、班车时刻无依据则留空。

ICS 固定活动用 `VEVENT`，DDL 用 `VTODO`，默认无闹钟。用户明确提前分钟数才加 `alarm_minutes`。不同日历支持 `VTODO` 的程度不同；导入后检查待办是否出现，不支持时保留 Markdown 截止清单，或按用户选择另建提前提醒事件。

生成文件不等于导入成功。宿主有日历／定时工具时按授权调用，核对返回状态后再说已创建。公开通知追踪任务只读取公开页面，不把私人日程发给信息源。

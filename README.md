# 乐师校园助手 · LSNU Campus Assistant

把乐山师范学院的校园问题变成**有出处、能执行的下一步**。面向 Marvis，也可由支持文件夹 Skill 的其他助手加载。

- **校园问答与办事清单**：19 张官方证据卡，覆盖重修、四六级、奖助、转专业概述、校历、双校区、图书馆与官方办事入口。
- **课表与 DDL**：周次展开、重叠检查、交通缓冲、弹性办事时段、ICS 导出；未知车程保留未知。
- **学业规划**：按本人培养方案核算已认定学分，重修记录不重复计分。
- **试讲与答辩**：逐轮提问、反馈和改进，适配师范与非师范学习任务。
- **成长复盘**：依据实际完成记录生成周报；预约、到场和完成分开。

知识核查至 **2026-09-23**。不是全校政策全集；实时课表、成绩、余座、校车以及未读到的制度附件，需官方当前信息或用户提供资料。范围见 [使用文档](docs/使用文档.md) 与 [验证报告](docs/验证报告.md)。

## 在 Marvis 安装

1. 下载仓库或解压发布包。
2. 确认导入文件夹顶层有 `SKILL.md`，保留 `scripts/`、`kb/`、`references/` 等子目录。
3. Marvis → 技能广场 → 工具箱 → 技能（Skill）→ 我的技能 → 导入技能 → 选择该文件夹。
4. 新建对话，选择此 Skill，发送：

> 请用乐师校园助手查询 2026 年秋季重修流程，给我报名截止、缴费注意事项和官方出处。

请导入整个文件夹；单个 `SKILL.md` 不包含知识与工具。宿主需本地文件读取权限；运行脚本需 Python 3.10+，无需 pip 包或 API key。

## 快速验证

仓库根目录运行：

```bash
python3 scripts/campus.py doctor
python3 scripts/campus.py search "四六级报名" --as-of 2026-09-23
python3 scripts/planner.py examples/plan-demo.json
python3 scripts/credits.py examples/credits-demo.json
python3 scripts/progress.py examples/progress-demo.json --start 2026-09-14 --end 2026-09-20
python3 -m unittest discover -s tests -v
```

示例均为虚构，不是真实课表、成绩或运动数据。脚本默认只输出结果。

## 学习与维护

- [使用文档](docs/使用文档.md)：安装、自然语言示例、数据保存与常见问题。
- [设计与高校借鉴](docs/设计与高校借鉴.md)：真实试用、源码机制和如何迁移。
- [验收题集](docs/验收题集.md)：可复用的正常与异常场景。
- [离线知识入口](references/offline-guide.md)：证据卡与原页。
- [贡献指南](CONTRIBUTING.md)：更新、验证与打包。
- [来源与授权](NOTICE.md)：代码、资料与借鉴项目。

`SKILL.md` 管决策和路由，`references/` 给按需工作流，`kb/` 存证据卡，`scripts/` 做确定性计算。个人记录放包外；本包无账号自动登录或密码采集逻辑。

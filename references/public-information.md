# GitHub Actions 公共信息采集与同步

每小时 UTC 第 17 分钟触发，也可在 GitHub Actions 手动运行。调度可能延迟，不能保证整点或秒级实时。默认超过三小时的快照标为过期；关键报名期限、费用、资格和开放安排仍直接核对官方原文。

流程为：学校公开页面 → Actions 采集 → 仓库 `public-knowledge` 分支的 `latest.json` → 本地公共缓存 → 当前 Agent 结合官方原文和已授权个人记忆回答。

## 采集范围和事实边界

固定入口见 `kb/public-sources.json`：教学部、学生资助、图书馆；每个入口最多发现八条实际出现的通知链接，同时复核 `kb/catalog.json` 已列来源。只访问乐师官方域名，不读取个人目录、浏览器会话或学校登录系统。

快照只公布标题、已识别的发布日期、官方 URL、正文指纹与成功／失败时间。不会复制学校全文、附件、个人记忆或用户问题。`published_on` 来自页面明确的发布日期字段，未知用 null；`collected_at` 是采集时间，不能代替政策生效时间或人工 `verified_on`。

新通知自动进入检索入口，旧页面指纹变化标记 changed。自动发现不等于内容已获维护者审核，脚本不改写人工证据卡中的资格、金额或截止日。回答时同时考虑原文权威性、适用对象、有效时间和替代关系。

## 本地调用

每次 `companion.py start` 在读取个人记忆之前，先检查程序版本并同步公共快照。二者独立：程序版本没有变化时仍同步新公告。

```bash
python3 scripts/companion.py start
python3 scripts/public_knowledge.py "图书馆 中秋 国庆" --snapshot "<上一步返回的 snapshot_path>"
```

根据命中 URL 回读官方通知。`content_revision` 只在公告内容、标题或发布日期变化时变化；`snapshot_sha256` 也覆盖本轮状态和采集时间。HTTPS 固定仓库是来源信任根，哈希用于完整性核验，不宣称数字签名。

同步失败保留上次有效快照，同时报告失败和快照年龄。原站失败保留上次内容指纹及 `last_success_at`，不会把失败时刻当作成功时间。来源部分失败时标为 partial；相关命中另有自己的 freshness。首页解析不到通知链接也记为失败，不假称没有新公告。

## Actions 运维

工作流 `.github/workflows/collect-public.yml` 只向 `public-knowledge` 分支更新 `latest.json`，不写主分支代码或个人数据。并发执行排队，避免互相覆盖；Git 推送失败会使作业失败。每次保留 14 天采集统计 artifact。连续失败时检查 Actions 的结果和对应原站，不放宽来源域名或伪造采集时间。

可在仓库 Actions 页面手动选择 **Collect public campus information → Run workflow**。本地复现可使用 `collect_public.py --output <包外路径> --previous <上次快照> --report <统计路径>`，输入来源固定从包内配置读取。

## 名称迁移

当前项目和 Skill 标识固定为 `lsnu-compus-skill`。1.0／1.1 的历史压缩包保留原名与原始校验值；旧标识客户端应重新导入新包，不能把新标识冒充旧版本。个人 Skill 的独立名称与包外记忆目录继续复用，迁移不授权读取或上传记忆。

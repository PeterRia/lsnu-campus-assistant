# 维护和贡献

## 更新资料

1. 打开官方正文和关联附件，核实发布主体、发布日期、适用对象、明确时段、替代关系。
2. 在 `kb/cards/` 写事实摘要、短引文、定位与限制；不收集学生名单和私人材料。
3. 更新 `kb/catalog.json` 元数据。`published_on` 不等于 `effective_from`；未知用 JSON `null`。真正复核后才能更新 `verified_on`。
4. `content_sha256` 是原页正文去空白后的 SHA-256，`source_snapshot_sha256` 是采集时原始页面摘要。未采集新指纹可留空，不伪造。
5. 页面变化或转为附件时人工复核；页面相同不排除有新通知。附件未读就标明。
6. 新增卡时更新 `references/offline-guide.md`、README 覆盖数，再运行：

```bash
python3 scripts/maintain.py refresh-hashes
python3 scripts/maintain.py check
python3 -m unittest discover -s tests -v
```

复核特定原页：

```bash
python3 scripts/campus.py check-sources --id retake-2026-autumn
```

命令不自动改卡片、不爬全站，返回正文供审阅。验证码与登录用正规用户流程。

## 维护节奏

开学核对校历、作息、校区服务和通知；报名期核对截止与变更。把知识缺口写明，不把昨天的“最新”长期保留。经验资料独立标来源，不能混成官方条件。

## 提交质量

- 确定性计算变更增加实际行为测试，避免只测标题措辞。
- 至少在宿主复测一个真实场景；脚本通过不代表模型回答通过。
- 不提交个人记录、凭据、会话导出、环境、缓存或其他学校语料。
- PR 写清问题、变化、依据和验证。

## 打包

```bash
python3 scripts/maintain.py package --output dist/lsnu-campus-assistant-1.1.0.zip
```

只打包明确允许的代码、知识和文档，附逐文件 SHA-256；不含 `.git`、研究原始资料、个人输出或环境。解压后选择内层 `lsnu-campus-assistant` 文件夹导入。

允许文件逐项列在 `PUBLIC-FILES.json`；新增文件须有意加入，不能以整个目录通配代替。符号链接、路径越界或清单缺失会阻止打包。`release.json` 是固定可信上游的更新入口，不放进 ZIP，以避免自引用校验。

```bash
python3 scripts/maintain.py release --output dist/v1.1.0 --manifest release.json
```

发布时先上传对应 GitHub Release 的 ZIP 和 SHA256SUMS，再使主分支的 `release.json` 生效，避免清单指向尚不存在的包。ZIP 内容必须与已验证代码一致。GitHub 的 HTTPS 仓库身份是信任根；本版没有冒充额外的签名机制。

个人 Skill 初始化后不由公共更新自动替换。其变更应单独审阅、保留个人修改与记忆，不得通过换一个公共版本覆写用户的本地授权。

对话式贡献流程只创建待审核 issue。维护者须核实来源、适用对象与隐私，再决定是否合并；个人经验保持经验标签。不得把用户点了“同意记忆”解释为“同意上传”。

## 优先扩展

读到当前官方来源后补充新版作息、完整转专业／学位／三字一话条款、校车与楼宇位置、专业培养方案。个人系统集成独立开发，以正规登录和真实回执验收，不能只添加“已支持”的提示词。

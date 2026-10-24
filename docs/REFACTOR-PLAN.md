# 项目改造方案 REFACTOR-PLAN

> 日期：2026-10-21 · 状态：已评审 · 参考：app/project/toutiao-ops-main

## 一、现状诊断（实测）

环境：playwright 1.64 已装、chromium 已下载、WSLg 可用、profiles 下有真实登录数据（122M/147M）。

库内实测：
- publish_tasks：1 成功 / 2 失败（code=7115 图片uri非法，10:48/10:49 连挂）/ 1 条永久 running（僵尸）。
- 百家号失败「账号还没有 cookie」：窗口登录过但纯 HTTP 驱动只认 cookie 字段。
- 2 个孤儿 profile 目录（58M / 4K）：删账号只删库不删目录。

### 5 个断点
1. 一个账号两套登录态（profile 活 + cookie 副本），无一致性保证；百家号纯 HTTP 只认 cookie。
2. 自动续期空转：accounts.py:609 开无头睡 6 秒抄 cookie，不产生新凭证；抄不到写 expired。
3. 无登录态巡检，只有手点「测登录」。
4. 发布同步阻塞（api.py:788），无重试/取消/幂等，僵尸任务。
5. 视频素材只统计后丢弃（base.py:271），10 平台预设只有 2 驱动。

## 二、参考项目借鉴

抄：扫码登录+二维码截图（auth.js:79-163）、独立 user_data_dir、stealth 反检测、ensureLoggedIn 三段等待（auth-guard.js:11-39）、dismissOverlays 清弹窗、数据采集三层降级（analytics.js:130-181）。
不抄：DOM 点击发布、无重试幂等、Node 运行时（WSL node v12 跑不了）。

## 三、目标架构

```
前端：热点资讯 | 生产流水线 | 发布中心 | 账号中心 | 系统维护
  │ REST + 轮询
api.py（只校验入队查询，不做长任务）
├ accounts/ sessions.py(profile 唯一真相源) login.py(扫码/有头/粘贴/导入) health.py(巡检) pool.py(会话池)
├ publish/  jobs.py(队列 worker) drivers/toutiao.py drivers/baijiahao.py
├ style.py  finish.py  cleanup.py  insight/
SQLite（_ensure_columns 迁移）
```

硬规则：profile 唯一真相源，cookie 仅供纯 HTTP 驱动内存使用；长任务不入 HTTP 请求；任务心跳+启动回收；登录判定只信平台接口。

## 四、需求1 同源风格规则

| 来源归一化 | 产出版本 |
|---|---|
| 头条（toutiao/今日头条/头条号/tt） | 仅百家版 |
| 百家（baijia/百家号/百度/bjh） | 仅头条版 |
| 其他/空 | 头条+百家两份 |

新增 style.py：targets_for(source)；别名进 app_settings。article_rewrites 加 target_platform / variant_group。提示词加 {{platform_style}}（头条=口语悬念短段强钩子；百家=书面高密度陈述多小标题）。标题只出对应平台那条；正文带约束；导出校验 match。

## 五、需求2 自动导出 output_finish

```
output_finish/tt/<标题>/文章.md+文章.txt+images/(1-2图)+video/(1个)+meta.json
output_finish/bjh/<标题>/ 同构
```

规则：图片 1-2 张、视频 1 个（finish.img_min/max、video_max，不复用 layout.*）；必选 match 最高（复用 layout.pick_assets，抽帧只兜底）；素材拷贝进目录写相对路径（现 layout.py:168 绝对路径）；目录名=该平台标题；导出按钮去掉、链路完成自动触发（articles.finish_status 幂等）。新增 finish.py（复用 layout/exporter）。

## 六、需求3 数据清理

现状：delete_batch 只删 cache+articles 不级联；无发布记录清理入口。

新增 cleanup.py + 维护页，先扫描（数量+占用）再执行：
- 数据源表格（级联 cache/media/rewrite/layout/output/output_finish）· 高
- 采样缓存（raw_cache+data/cache，fetch_status 复位）· 中
- 素材缓存（media_assets 无引用文件）· 中
- 发布记录（按时间/状态/平台）· 低
- 改写排版历史（保留最新 1 条/平台）· 中
- 孤儿文件（output/、data/media、data/cache、data/profiles 磁盘有库无）· 中
- 账号数据目录 · 中 · 日志轮转 · 低

要点：单事务；白名单根目录校验防路径篡改；返回 {rows,files,bytes}；VACUUM 按钮。

## 七、需求4 账号删除连 profile

delete(account_id, purge_profile=True, purge_tasks=False)：先停运行中会话 → profile 白名单校验 → 删库（可选连带 publish_tasks）→ rmtree 返回释放空间。
UI 两档确认；清理中心可批量清无主 profile。

## 八、账号/登录/发布重构

- 登录主路径改扫码：截图二维码→前端展示→轮询→落 profile→立即校验。二维码多档选择器+整页兜底；50s 刷新最多 6 次；成功必须问平台接口。
- 自动续期改巡检：间隔验活，失效置顶告警，不做无意义续期。
- 发布任务化：API 只入队返回 job_id；worker+心跳+启动回收+退避重试+取消+单账号串行锁。
- 平台保持头条+百家，驱动契约补 check_auth/upload_image/publish/list_content/analytics。
- 视频不做直传，导出到 video/ 供人工上传。

## 九、数据分析（新增 insight/）

| 数据 | 方式 | 用途 |
|---|---|---|
| 作品列表 | 内容管理页响应劫持 | 审核状态 |
| 单篇详情 | GET /mp/agw/statistic/v2/item/info | 阅读/评论/收藏 |
| 账号概览 | 响应劫持+DOM 正则 | 粉丝/涨粉/收益 |
| 粉丝分布 | React Fiber/echarts option | 性别年龄地域 |

新增表：insight_snapshots（账号×日期）、insight_works（作品维度）。复用登录会话，只读安全。高阅读特征反哺改写提示词。

## 十、UI 设计

Tab 改 5 个：热点资讯（保留，加四段状态点+来源分布）/ 生产流水线（四列看板：待抓/采素材/改写/待复核，一键全链路）/ 发布中心（任务表+进度+原因+重试取消）/ 账号中心（状态灯置顶+扫码弹窗+更多下拉）/ 系统维护（设置+风格规则+数据清理+账号数据）。
热点资讯：去掉导出按钮，改为「查看产物」。
发布中心：7115 等错误给可读建议；失败可重试；登录态灯过期直接「去扫码」。
账号中心：扫码弹窗显示二维码+倒计时+「已扫码检测中」。
维护-数据清理：3 卡片+1 危险区，先扫描后清理。

## 十一、实施排期（6 阶段）

1. 账号与登录重构：扫码登录、profile 唯一真相、百家号 cookie 派生、删除连 profile。
2. 同源规则+改写引擎分平台（style.py + rewriter）。
3. 自动导出 output_finish（finish.py，去导出按钮）。
4. 数据清理中心（cleanup.py + 维护页）。
5. UI 重构（流水线/发布中心/账号中心/维护）。
6. 数据分析模块（insight/ + 看板）。

## 十二、风险

1. 来源字段不规范：别名可配，兜底其他→两份。
2. 标题字数：头条 2-30 字提示词约束；目录名安全处理。
3. 删除安全：白名单根目录强制校验。
4. 单账号并发：chromium 单例锁，账号级串行锁。
5. 扫码登录元素变动：多档选择器+整页截图兜底，选择器集中配置。

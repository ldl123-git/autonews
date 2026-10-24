# 改造实施进度 REFACTOR-TODO

> 方案见 `docs/REFACTOR-PLAN.md`。状态更新：2026-10-21

## 已完成

### 阶段 1 账号与登录重构
- [x] 扫码登录后端 `accounts.start_login`：无头开登录页 → 多档选择器截二维码 → 存 `data/qrcode/` → 轮询 → 自动落 cookie/profile → 立即校验
- [x] 已有登录态直接同步（不再空等扫码）：profile 里有会话 cookie 且不在登录页时直接落库
- [x] API：`POST /accounts/{id}/login`、`GET /accounts/{id}/login/qr`
- [x] 前端扫码弹窗：二维码 + 状态轮询 + 取消（`qr_at` 做图片缓存键，避免闪烁）
- [x] 百家号 cookie 派生：`sync_cookie_from_profile` + 驱动 `_sync_from_profile`，不再要求手动粘 cookie
- [x] 自动续期改巡检：`probe_health` + `_tick_auto` 按 last_checked_at 判到期，只验活不改值
- [x] 账号删除连 profile：`delete(delete(account_id, purge_profile=True, purge_tasks=False))`，白名单校验 + rmtree + 释放空间
- [x] stealth 启动参数：`--disable-blink-features=AutomationControlled`、`ignoreDefaultArgs=['--enable-automation']`、zh-CN / Asia/Shanghai
- **实测**：账号 11 扫码登录 30 秒内自动同步登录态并回写昵称；新建账号 6 秒出二维码（12.5KB PNG）；删除账号释放 43MB

### 阶段 2 同源规则 + 改写分平台
- [x] `app/style.py`：`normalize_source` / `targets_for` / `style_brief` / `explain`
- [x] 别名可配置：`style.rule_mode`、`style.tt_aliases`、`style.bjh_aliases`（设置页「风格规则」）
- [x] 导入时写 `articles.source_norm`
- [x] `article_rewrites` 加 `target_platform` / `variant_group`
- [x] 改写按平台产出：标题只取对应平台那条，正文追加平台风格约束（兼容老提示词）
- [x] `GET /rewrite/variants/{article_id}` 多版本查询
- **实测**：头条→仅百家版、百家→仅头条版、其他/空→两版；`baijiahao.baidu.com` 正确判为百家

### 阶段 3 自动导出 output_finish
- [x] `app/finish.py`：`output_finish/{tt,bjh}/<标题>/{文章.md,文章.txt,images/,video/,meta.json}`
- [x] 素材规则：图片 1~2 张、视频 1 个，取 match 最高（复用 `layout.pick_assets`）
- [x] 素材拷进目录 + Markdown 相对路径（可整体打包）
- [x] 幂等：`articles.finish_status`；改写完成 / 素材就绪谁后完成谁触发
- [x] 前端导出按钮改为「重新交付」
- **实测**：文章 71 导出 tt/bjh 双目录，2 张图（118KB/89KB）落地，相对路径引用正确

### 阶段 4 数据清理中心
- [x] `app/cleanup.py`：scan + run_batches/run_cache/run_media/run_publish/run_rewrites/run_orphans/run_profiles/run_logs/vacuum
- [x] 安全：单事务、白名单根目录校验、平台目录（tt/bjh）永不误判为孤儿
- [x] API：`GET /cleanup/scan`、`POST /cleanup/run`、`POST /cleanup/vacuum`
- [x] 前端「系统维护 → 数据清理」：3 卡片 + 先扫描（数量/占用）后清理
- **实测**：扫描 0.3 秒，识别 7 批次 / 11 缓存 / 2 孤儿输出 / 2 无主 profile（31MB）

### 阶段 5 UI 重构
- [x] 6 个 Tab：热点资讯 / 生产流水线 / 发布中心 / 账号中心 / 抓取缓存区 / 系统维护
- [x] 生产流水线：四列看板（待抓 / 采素材 / 改写 / 已交付）+ 队列计数 + 一键推进
- [x] 发布中心：任务表 + 状态/时间/失败原因 + 打开 + 删除
- [x] 账号中心：扫码登录为主按钮 + 「更多」折叠菜单
- [x] 系统维护：大模型配置 / 运行环境 / 数据清理
- **实测**：6 视图切换正常，清理页 8 项渲染，控制台 0 错误

### 阶段 6 数据分析
- [x] `app/insight.py`：作品列表 / 账号概览 / 粉丝分布（页面响应劫持 + 账号信息接口兜底）
- [x] 表 `insight_snapshots` / `insight_works`
- [x] API：`POST /insight/collect/{id}`、`GET /insight/overview`
- [x] 账号中心「看数据」弹窗：账号指标 + 作品数据 + 空状态说明
- **实测**：账号 10 采集命中 works 15 / overview 10 / fans 10，回写昵称「舟上钓江鱼」与 media_id

## 第二轮：发布任务化（已完成）

- [x] `app/publish/jobs.py`：asyncio 队列 + 常驻 worker（并发 2，同账号串行加锁）
- [x] `POST /publish` 改为只入队，立刻返回 `tasks[{task_id, status:pending}]`
- [x] 心跳：worker 每 5 秒写 `heartbeat_at`
- [x] 僵尸回收：启动时把 `running` 且心跳断 10 分钟的任务标 `failed`（可重试）
- [x] 重试 `POST /publish/tasks/{id}/retry`、取消 `POST /publish/tasks/{id}/cancel`
- [x] 任务详情 `GET /publish/tasks/{id}`
- [x] 前端：提交后轮询任务状态；发布中心加「重试 / 取消」按钮
- [x] 7050 失败提示补充「请完善账号信息」真实场景
- **实测**：提交立即返回 pending → 3 秒 running → 15 秒按真实业务结果落 failed；重试后 attempts 递增并重新执行；发布中心 0 JS 错误

## 仍未做

- [ ] 视频直传（按决策不做；导出已放 `video/` 供人工上传）
- [ ] 百家号数据采集（页面结构差异大，当前只做头条）
- [ ] 账号级 profile 单账号清理入口（账号删除已覆盖，清理中心只有批量无主清理）

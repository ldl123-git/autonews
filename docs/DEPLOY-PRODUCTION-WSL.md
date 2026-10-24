# autonews 生产部署手册（Windows + WSL2 Ubuntu）

> 部署形态：代码放在 Windows 的 **`E:\autonews`**（WSL 内即 `/mnt/e/autonews`），应用跑在 WSL2 的 Ubuntu 里，
> 监听 `0.0.0.0:8000`，局域网内用浏览器访问。
> 参考环境：Windows 10/11 + WSL2 / Ubuntu 22.04.5 / Python 3.10.12 / ffmpeg 4.4.2 / Playwright chromium-1248。

## 0. 本次部署约定

| 项 | 决定 | 说明 |
|---|---|---|
| 项目路径 | `/mnt/e/autonews`（= `E:\autonews`） | **生产机必须有 E 盘**。`deploy/*.sh` 默认路径就是这个，无需传 `APP_DIR` |
| 历史数据 | 不带 | 不拷 `data/` `output/` `output_finish/`；首次启动自动建空库，LLM Key / 账号 / 设置重新配 |
| 浏览器登录态 | 不带，目录自动创建 | `/var/lib/autonews/profiles` 由程序首次登录时自动 `mkdir`（以 root 运行即可自动创建） |
| 监听地址 | `0.0.0.0:8000` | 局域网可访问；**应用无鉴权**，必须配防火墙限制来源（见第 8 节） |
| 代码来源 | 推送到 `github.com/ldl123-git/autonews` | 生产机优先「整目录拷贝」，git clone 作为备用渠道 |

## 1. 环境要求

- Windows 已装 WSL2 + Ubuntu（`wsl -l -v` 能看到，状态 Running/Stopped 均可）
- **E 盘存在**，剩余空间 ≥ 20 GB（素材/成稿会持续增长）
- 能访问外网：抓新闻站点、搜索引擎、LLM 接口、百家号/头条
- 系统依赖：Python ≥ 3.10、ffmpeg/ffprobe、Playwright + chromium

关键事实（决定了后面怎么起服务）：

| 事实 | 后果 |
|---|---|
| 单进程 FastAPI + 进程内后台引擎（抓取 / 改写 / 素材 / 发布队列） | **只能跑一个实例**，禁止 `--workers N`，多开会互抢 SQLite 和任务 |
| 前端是 `web/` 下纯静态文件 | 不需要 Node/npm/构建 |
| SQLite 单文件 `data/app.db` | 启动自动建表 + 补列迁移，不用手工执行 SQL |
| 扫码登录 / 发布走 Playwright 无头浏览器 | 不需要桌面环境或 WSLg；必须额外装 playwright + chromium |
| 项目在 `/mnt/e`（NTFS 挂载） | SQLite 的 WAL 会自动降级、IO 比 ext4 慢，所以**必须单实例**，别叠加并发压力 |

## 2. 开发机：代码推送到 GitHub

仓库：`https://github.com/ldl123-git/autonews.git`（账号 `x862456735@163.com`）

```bash
cd /mnt/e/autonews

# 1) 首次初始化（仓库已 init 过就跳过 init）
git init
git config user.name "autonews"
git config user.email "x862456735@163.com"

# 2) 确认 .gitignore 生效：data/ output/ output_finish/ upload/ 都不入库
git check-ignore -v data output output_finish

# 3) 提交（仓库为空时用 main 分支）
git add -A
git commit -m "init: autonews 资讯采集/改写/素材/发布"
git branch -M main

# 4) 关联远程
git remote add origin https://github.com/ldl123-git/autonews.git

# 5) 推送
git push -u origin main
```

> **GitHub 已经不支持用账号密码做 git 推送**（2021 年起），第 5 步要 Personal Access Token：
> GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** →
> Generate new token → 勾 `repo` → 复制 token。
> 推送时提示输入凭据：username 填 `x862456735@163.com`，password 填 **token**（不是登录密码）。
>
> 更安全的两点：① 不要把密码/token 写进任何文件（`.git/config` 里出现 `https://user:token@...` 等于泄露）；
> ② 登录密码已出现在聊天里，建议尽快在 GitHub 上改掉，之后只用 token。

推送后核对（浏览器能看到 42+ 个文件，没有 `data/` `output_finish/`）：

```bash
git log --oneline -1
git ls-files | wc -l
git remote -v
```

## 3. 生产机：准备目录

```powershell
# 生产机 PowerShell（管理员）
wsl --list --verbose          # 确认发行版名字，记下（例如 Ubuntu）
wsl -d Ubuntu -- bash -lc "python3 -V && ffmpeg -version | head -1"
```

E 盘确认：

```powershell
Get-PSDrive E | Select-Object Name,Used,Free
```

没有 E 盘就先建好 `E:\`（或在磁盘管理里把某个分区挂成 E:），否则 `/mnt/e/autonews` 不存在。

## 4. 把代码拷到生产机（不带历史数据）

三选一，**推荐方式 A**（Windows 侧直接复制，最直观）。

### A. robocopy（推荐）

在**生产机**上执行，把开发机 `E:\autonews` 共享出来（或在开发机执行反向推送都可以），
等价于「只拷代码，不拷运行数据」：

```powershell
# 生产机 PowerShell（源为共享 \\DEVPC\autonews，也可换成 U 盘盘符）
robocopy \\DEVPC\autonews E:\autonews /MIR ^
  /XD data output output_finish upload .git .codebuddy __pycache__ ^
  /XF *.pyc *.log *.db /NFL /NDL /NJH
```

几点说明：

- `/XD data output output_finish` —— 正是「不带历史数据」；这三个目录缺失时程序启动会自动创建。
- `/XD .git` —— 生产机不需要仓库历史，少一层麻烦（要升级就再拷一次）。
- 纯 U 盘拷贝：直接把 `E:\autonews` 复制到生产机 `E:\`，然后**手动删掉** `data` `output` `output_finish` 三个目录。
- 装完之后目录里应有：`app/ deploy/ docs/ web/ requirements.txt run.py`，**没有** data/output/output_finish。

### B. 打一个干净 tar 包（适合跨网/U 盘）

开发机：

```powershell
wsl -- bash -lc "cd /mnt/e && tar --exclude='autonews/data' --exclude='autonews/output' --exclude='autonews/output_finish' --exclude='autonews/.git' --exclude='autonews/.codebuddy' --exclude='*/__pycache__' --exclude='*.pyc' --exclude='*.log' -czf /mnt/e/autonews-code.tar.gz autonews"
```

包约 6 MB。拷到生产机后：

```powershell
wsl -- bash -lc "cd /mnt/e && tar -xzf /mnt/e/autonews-code.tar.gz && ls /mnt/e/autonews"
```

### C. git clone（备用，需要网络能到 GitHub）

```powershell
wsl -- bash -lc "cd /mnt/e && rm -rf autonews-clone && git clone https://github.com/ldl123-git/autonews.git autonews-clone && ls autonews-clone"
# 确认没问题后再替换：把 autonews-clone 里的内容移动到 /mnt/e/autonews
```

私有仓库需要 PAT（同第 2 节）；公开仓库直接 clone。

## 5. 安装依赖

```bash
# 5.1 系统依赖
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg
python3 -V                        # 需 ≥ 3.10
ffmpeg -version | head -1

# 5.2 Python 依赖（项目不使用 venv，与 deploy/wsl_setup.sh 一致）
cd /mnt/e/autonews
sudo python3 -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 5.3 发布/扫码登录必需（不在 requirements.txt 里）
sudo python3 -m pip install playwright -i https://mirrors.aliyun.com/pypi/simple/
sudo python3 -m playwright install-deps chromium
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ python3 -m playwright install chromium
```

也可以一条命令装完系统依赖 + requirements（脚本幂等，**不含 playwright**）：

```bash
cd /mnt/e/autonews && sudo -E bash deploy/wsl_setup.sh
```

> `pip` 用 sudo（装到系统 site-packages）；`playwright install chromium` **不要 sudo**，
> 浏览器会落在当前用户 `~/.cache/ms-playwright`。服务最终以哪个用户跑，浏览器就装在哪个用户下
> —— 三处（pip、浏览器、服务）保持一致。Ubuntu 23.04+/24.04 的 pip 需加 `--break-system-packages`，
> `deploy/wsl_setup.sh` 会自动判断。

自检：

```bash
python3 -c "import fastapi, uvicorn, httpx, PIL, openpyxl, xlrd, zxingcpp; print('deps ok')"
python3 -c "import playwright; print('playwright ok')"
ffmpeg -encoders 2>/dev/null | grep -E " (libx264|aac) "
```

## 6. 首次启动

```bash
cd /mnt/e/autonews
bash deploy/wsl_start.sh            # 默认 HOST=0.0.0.0 PORT=8000 TZ=Asia/Shanghai，先杀旧进程再起
tail -n 30 data/server.log
```

等价裸命令：

```bash
cd /mnt/e/autonews && HOST=0.0.0.0 PORT=8000 TZ=Asia/Shanghai setsid nohup python3 run.py > data/server.log 2>&1 &
```

首次启动会自动完成（**不需要手工建库/建目录**）：

- `data/` `data/uploads` `data/cache` `data/qrcode` `output/` `output_finish/` 目录
- `data/app.db` 建表 + 列迁移
- 敏感词自动机预热、各类后台引擎与发布 worker 拉起
- `/var/lib/autonews/profiles` 在第一次扫码登录时创建（root 运行可直接创建）

## 7. 验收清单

1. WSL 内接口通：`curl -s http://127.0.0.1:8000/api/stats | head -c 200`
2. Windows 本机浏览器打开 `http://localhost:8000`
3. 局域网其他机器打开 `http://<生产机IP>:8000`（需先做第 8 节）
4. 系统设置 → LLM 配置填 Key → 「测试连通」正常
5. 发布账号 → 「扫码登录」：页面上出二维码，手机扫码后自动落库
6. 跑通一篇：抓取 → 改写 → 素材（图 + 视频）→ 排版 → 交付/发布
7. 成片必须有音轨（百家号上传要求）：

```bash
ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "/mnt/e/autonews/output_finish/bjh/<标题>/0001.mp4"
# 期望两行：video / audio
```

8. `data/server.log` 里没有异常栈

## 8. 局域网访问（HOST=0.0.0.0）

WSL2 默认 NAT 模式：Windows 本机走 localhost 没问题，但**局域网访问不到 WSL 端口**，二选一。

### 方式一：Win11 22H2+ 镜像网络（推荐，不用维护 IP）

`C:\Users\<你>\.wslconfig`：

```ini
[wsl2]
networkingMode=mirrored
```

`wsl --shutdown` 后生效。之后 WSL 直接共用 Windows 网卡，`0.0.0.0:8000` 即对局域网可见，只差防火墙：

```powershell
# 管理员 PowerShell：只放行办公网段
New-NetFirewallRule -DisplayName "autonews 8000" -Direction Inbound -Protocol TCP `
  -LocalPort 8000 -Action Allow -RemoteAddress 192.168.0.0/16
```

### 方式二：Windows 10 / 旧版用 portproxy

```powershell
# 管理员 PowerShell
$wslIp = (wsl -d Ubuntu -- hostname -I).Trim().Split()[0]
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8000 connectaddress=$wslIp connectport=8000
netsh interface portproxy show v4tov4
New-NetFirewallRule -DisplayName "autonews 8000" -Direction Inbound -Protocol TCP `
  -LocalPort 8000 -Action Allow -RemoteAddress 192.168.0.0/16
```

> WSL 的 IP 每次重启会变，portproxy 需要重新执行；可以把这个片段存成 `D:\deploy\refresh-proxy.ps1`，
> 加到开机任务里。

### 安全（务必看）

应用**没有登录鉴权**：能连上 8000 的人可以删数据、改配置、直接调发布接口。
所以：`0.0.0.0` 只在可信内网使用，防火墙务必限定来源网段；**绝对不要把 8000 直接映射到公网**。
将来要外网访问，请在前面加反向代理 + Basic Auth / IP 白名单。

## 9. 开机自启

WSL 的 `/etc/wsl.conf` 已开 systemd（`[boot] systemd=true`），用 service 管理最稳：

```bash
sudo tee /etc/systemd/system/autonews.service >/dev/null <<'EOF'
[Unit]
Description=autonews (Windows+WSL)
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/mnt/e/autonews
Environment=HOST=0.0.0.0
Environment=PORT=8000
Environment=TZ=Asia/Shanghai
Environment=AUTONEWS_PROFILE_ROOT=/var/lib/autonews/profiles
ExecStart=/usr/bin/python3 run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now autonews
systemctl status autonews --no-pager
```

WSL 实例要有人拉起，所以 Windows 侧再加个登录触发器。新建 `D:\deploy\autonews-boot.cmd`：

```bat
@echo off
C:\Windows\System32\wsl.exe -d Ubuntu -u root --exec /bin/systemctl start autonews
```

```powershell
# 管理员 PowerShell
schtasks /Create /TN "autonews-boot" /SC ONLOGON /RL HIGHEST /F /TR "D:\deploy\autonews-boot.cmd"
schtasks /Query /TN "autonews-boot" /V /FO LIST
```

不用 systemd 就把 `.cmd` 第二行换成直接起脚本（启动脚本自带杀旧进程，幂等）：

```bat
@echo off
C:\Windows\System32\wsl.exe -d Ubuntu -u root -- bash -lc "bash /mnt/e/autonews/deploy/wsl_start.sh"
```

`Ubuntu` 换成 `wsl -l -q` 里的实际发行版名。

## 10. 日常运维

```bash
# 启停（systemd 方式）
sudo systemctl start autonews
sudo systemctl stop autonews
sudo systemctl restart autonews
journalctl -u autonews -n 100 --no-pager

# 脚本方式（无 systemd）
cd /mnt/e/autonews && bash deploy/wsl_start.sh      # 启动/重启
bash deploy/wsl_stop.sh                              # 停止

# 日志
tail -n 200 /mnt/e/autonews/data/server.log

# 进程检查：必须只有 1 个
pgrep -af "python3 run.py"
```

**备份**（生产跑起来后就有数据了，建议每天或每次批量处理前备一次）：

```bash
sudo systemctl stop autonews
mkdir -p /mnt/d/backup
tar -czf /mnt/d/backup/autonews-$(date +%F).tar.gz -C /mnt/e/autonews data output_finish
sudo systemctl start autonews
```

**升级**（重新拷代码，不动 `data/` `output/` `output_finish/`）：

```bash
sudo systemctl stop autonews
tar -czf /mnt/d/backup/data-$(date +%F-%H%M).tar.gz -C /mnt/e/autonews data
# 从开发机重新执行第 4 节的 robocopy / tar 包覆盖 E:\autonews（排除 data output output_finish）
sudo python3 -m pip install -r /mnt/e/autonews/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
sudo systemctl start autonews
tail -n 30 /mnt/e/autonews/data/server.log     # 启动时会自动补列迁移
```

**回滚**：停服 → 用上一版代码覆盖 → 还原 `data` 备份 → 启动。

## 11. 排障速查

| 现象 | 排查方向 |
|---|---|
| 本机 `localhost:8000` 打不开 | WSL 内先 `curl 127.0.0.1:8000`；再查 `ss -lntp \| grep 8000`、`pgrep -af run.py` |
| 局域网打不开、本机正常 | 没做第 8 节的 mirrored/portproxy，或防火墙没放行；`netsh interface portproxy show v4tov4` |
| 页面能开但任务不动/状态乱跳 | 多实例了。`pgrep -af run.py` 只允许 1 个；systemd 与 wsl_start.sh 不要同时用 |
| 扫码报「未安装 playwright」 | `pip install playwright` + `playwright install chromium`，且与运行用户一致 |
| 二维码一直「正在打开登录页」 | 平台登录页出网被拦；必要时在设置里配 `browser.proxy`，看 `server.log` |
| `/var/lib/autonews/profiles` 创建失败 | 用普通用户跑服务但没权限 → `sudo mkdir -p /var/lib/autonews/profiles && sudo chown $USER /var/lib/autonews`，或设 `AUTONEWS_PROFILE_ROOT=$HOME/autonews-profiles` |
| 视频处理失败 | `ffmpeg -version`；设置页 `media.ffmpeg`；确认 `libx264` / `aac` 编码器在 |
| 成片没声音 / 百家号报 `NO_AUDIO` | 已默认保留原音轨；旧素材需重跑或补静音轨 |
| `database is locked` | `/mnt/e` 上 WAL 不可用 + 多实例；确保单实例，别并发跑大批量 |
| 日志时间不对 | 启动环境要 `TZ=Asia/Shanghai`（systemd 的 `Environment=` 已带） |
| E 盘空间吃紧 | `output/` `output_finish/` 累积；定期归档到其他盘，或在页面里清理历史文章 |

## 12. 部署检查清单

- [ ] 生产机 WSL2 + Ubuntu 可用，**E 盘存在**
- [ ] 代码已拷到 `E:\autonews`（`/mnt/e/autonews`），且**没有** `data/` `output/` `output_finish/`
- [ ] `python3 -V` ≥ 3.10；`ffmpeg -version` 正常
- [ ] `requirements.txt` 装完；playwright + chromium 装完（同一用户）
- [ ] `bash deploy/wsl_start.sh` 起得来，`data/app.db` 自动生成
- [ ] `curl 127.0.0.1:8000/api/stats` 有返回；本机 `localhost:8000` 能打开
- [ ] 局域网访问已配（mirrored 或 portproxy）+ 防火墙仅放行内网网段
- [ ] 系统设置里 LLM Key 配好，「测试连通」通过
- [ ] 至少一个发布账号扫码登录成功（profile 目录自动创建成功）
- [ ] 跑通一篇全流程，成片 `ffprobe` 有 `audio` 流
- [ ] systemd 服务已 `enable` + Windows 登录触发器已加，整机重启后自动可用
- [ ] 备份目录可用，备份命令试跑过一次
- [ ] 代码已推送到 `github.com/ldl123-git/autonews`（不含 data/output/output_finish）

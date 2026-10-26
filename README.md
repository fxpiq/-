# LiveClip：Ubuntu 24.04 抖音直播录像与 AI 切片系统

这是一个面向单服务器、单路直播的可运行实现。输入抖音直播间地址后，系统解析直播流并用 FFmpeg 分段录像；录像结束后使用 Faster-Whisper 识别中文、自动挑选高光、烧录字幕，输出适合抖音的 1080×1920 MP4。

> 请只录制和发布你拥有授权的内容。抖音页面结构、直播风控和登录要求会变化，系统不绕过付费、隐私、DRM 或其他访问限制。

## 1. 能力边界

- 单路录制，重复启动会返回 `409`。
- 手动输入 `live.douyin.com` 或 `v.douyin.com` 地址。
- `live.douyin.com` 优先使用直播页原生流解析，不依赖 yt-dlp 的普通视频提取器。
- 直播流直接复制封装，不重新编码，CPU 开销低。
- 每 5 分钟写一个 MPEG-TS 分段；停止后合并为 MKV。
- 默认最长录制 4 小时，磁盘少于 20 GB 时拒绝开始。
- 录制中每 30 秒检查一次磁盘，低于安全线时自动停止并保留已写入内容。
- 磁盘安全线会按已录制分段大小动态增加，确保仍有余量完成最终 MKV 合并。
- AI 完整链路：中文语音识别 → 本地高光评分 → 可选 LLM 二次挑选 → ASS 字幕 → 9:16 模糊背景短视频。
- Whisper 模型优先从 ModelScope 国内镜像下载，避免 Hugging Face/Xet 网络不可达。
- 控制台显示模型下载、音轨提取、语音识别、选段和切片渲染的阶段百分比。
- 长录像按 10 分钟音频分段转写，控制峰值内存，避免 2 GB 小内存服务器被 OOM Killer 终止。
- 没配置 LLM 密钥也能完整工作；本地评分是默认路径。
- SQLite 保存任务状态，文件保存在宿主机 `data/`。
- 控制台和 API 使用 HTTP Basic Auth，未做多用户权限系统。

## 2. 架构

```text
浏览器控制台
    │ Basic Auth + JSON API
    ▼
FastAPI（单 Uvicorn worker）
    ├── Recorder 线程 ── yt-dlp 解析 ── FFmpeg 分段录制
    │                                      └── TS → MKV
    ├── SQLite 任务/切片元数据
    └── ClippingPipeline 线程
          ├── Faster-Whisper 中文转写
          ├── 关键词/语速/标点/时长评分
          ├── 可选 OpenAI 兼容接口精排
          ├── ASS 字幕
          └── FFmpeg H.264 + AAC 竖屏切片
```

为什么不用消息队列：需求只有 1 路录像，SQLite + 两个后台线程已经覆盖状态、失败重试和串行切片。增加 Redis/Celery 会提升运维复杂度，目前没有对应收益。若以后扩到多路，再将 `Recorder` 和 `ClippingPipeline` 拆成独立 worker，并用 Redis Streams 或 Celery 分发。

## 3. 项目目录

```text
liveclip/
├── app/
│   ├── main.py              # FastAPI、登录、REST API
│   ├── recorder.py          # 单路直播解析、录像、停止、合并
│   ├── pipeline.py          # 转写、高光选择、切片编排
│   ├── transcriber.py       # Faster-Whisper
│   ├── highlights.py        # 本地候选评分与 LLM 结果校验
│   ├── subtitles.py         # ASS 字幕和 FFmpeg 滤镜
│   ├── db.py                # SQLite
│   ├── config.py            # 环境变量
│   └── static/              # 中文控制台
├── deploy/
│   ├── install.sh           # Ubuntu 一键安装
│   ├── liveclip.service     # 可选：systemd 包装 Docker Compose
│   └── nginx.conf           # 可选：域名和 HTTPS 反向代理
├── scripts/check_runtime.sh # 容器内自检
├── tests/test_core.py       # 高光、字幕、URL 单元测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

运行后数据目录：

```text
data/
├── liveclip.db
└── recordings/<任务ID>/
    ├── recording.mkv
    ├── clips/clip_01.mp4 ...
    └── work/
        ├── transcript.json
        ├── candidates.json
        └── clip_01.ass ...
```

## 4. Ubuntu 24.04 安装

将下面示例中的 `YOUR_SERVER_IP` 替换为你的服务器地址。不要把 root 密码写进脚本、Git 仓库或聊天记录。登录时由终端提示输入：

```bash
ssh -p 22 root@YOUR_SERVER_IP
```

建议先完成基础更新：

```bash
apt-get update
apt-get -y upgrade
timedatectl set-timezone Asia/Shanghai
apt-get install -y unzip ca-certificates
```

把交付的 `liveclip-ubuntu24.04.zip` 上传到服务器。可以在你自己的电脑执行：

```text
scp -P 22 liveclip-source.zip root@YOUR_SERVER_IP:/opt/
```

登录服务器后：

```bash
mkdir -p /opt
cd /opt
unzip -o liveclip-source.zip
cd /opt/liveclip
bash deploy/install.sh
```

安装脚本会完成这些动作：

```bash
apt-get install -y docker.io docker-compose-v2 ffmpeg fonts-noto-cjk openssl ufw
systemctl enable --now docker
mkdir -p data secrets
chown -R 10001:10001 data
chown -R 10001:10001 secrets
docker compose up -d --build
```

同时会生成随机管理员密码、创建 4 GB Swap（服务器内存较小且尚无 Swap 时），并设置数据目录权限。

查看密码和运行状态：

```bash
cd /opt/liveclip
grep '^ADMIN_PASSWORD=' .env
docker compose ps
docker compose logs -f --tail=200
```

浏览器打开：

```text
http://YOUR_SERVER_IP:8000
```

用户名默认是 `admin`，密码使用 `.env` 中的随机值。首次打开浏览器会弹系统登录框。

### 安全组和防火墙

云厂商安全组需要放行 TCP `22` 和 TCP `8000`。如果服务器启用了 UFW：

```bash
ufw allow 22/tcp
ufw allow 8000/tcp
ufw --force enable
```

HTTP Basic Auth 在纯 HTTP 下不是加密传输。长期生产使用建议绑定域名、启用 HTTPS，或只允许固定办公 IP 访问。域名准备好后可以：

```bash
apt-get install -y nginx certbot python3-certbot-nginx
sed -i 's/^BIND_ADDRESS=.*/BIND_ADDRESS=127.0.0.1/' /opt/liveclip/.env
docker compose up -d
sed 's/live.example.com/你的域名/' /opt/liveclip/deploy/nginx.conf \
  > /etc/nginx/sites-available/liveclip
ln -sf /etc/nginx/sites-available/liveclip /etc/nginx/sites-enabled/liveclip
nginx -t && systemctl reload nginx
certbot --nginx -d 你的域名
```

## 5. 使用流程

1. 在抖音网页或客户端复制正在直播的直播间链接。
2. 在控制台输入链接和任务名，点击“开始录像”。
3. 等待状态从“准备中”变为“录制中”。
4. 结束后点击“停止录像”。系统先停止 FFmpeg，再合并分段；不要在这一步强制重启容器。
5. 状态变为“录像完成”后，选择数量和单条时长，点击“AI 生成切片”。
6. 首次切片会下载 Whisper 模型，完成后生成 `clips/clip_01.mp4`。
7. 在页面预览或下载切片。`work/transcript.json` 可用于复盘识别质量。

建议录制前先用 1–2 分钟短测。正式录制会直接保存平台原始编码，长录像的码率取决于直播源。

控制台顶部会实时显示当前是否正在录制。已结束任务可以点击“删除任务”永久删除录像、切片和记录；也可以使用“清理已结束任务”批量清理。正在录制或正在生成切片的任务会被系统拒绝删除。

## 6. 抖音登录和 cookies

公开直播间通常直接可解析，`live.douyin.com` 不需要 cookies。只有直播页明确要求登录、年龄确认、地区验证，或返回人机验证时，才需要配置 cookies：

1. 只在你有权使用的账号下导出 Netscape 格式 cookies。
2. 将文件保存为 `/opt/liveclip/secrets/cookies.txt`。
3. 权限设为仅 root 可读：

```bash
chown 10001:10001 /opt/liveclip/secrets/cookies.txt
chmod 600 /opt/liveclip/secrets/cookies.txt
docker compose restart liveclip
```

`secrets/` 以只读方式挂进容器。cookies 属于登录凭据，不能提交到 Git、工单或公开网盘。cookies 过期后需要重新导出。不要使用绕过平台限制的方式。

## 7. AI 和字幕配置

默认使用 CPU：

```dotenv
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_CHUNK_SECONDS=600
WHISPER_BEAM_SIZE=3
```

模型选择：

| 模型 | CPU 内存/速度 | 适用场景 |
|---|---|---|
| `small` | 较轻，默认推荐 | 普通普通话直播，4C8G 起步 |
| `medium` | 更慢、约需更多内存 | 术语多、普通话不标准的内容 |
| `large-v3` | 建议 GPU | 高精度批量生产 |

首次使用某个模型时会从 ModelScope 下载到 Docker 模型卷，后续切片直接复用。若使用自定义模型仓库，可设置 `WHISPER_MODELSCOPE_ID`；下载中断后会保留 `.part` 文件并在下次继续。

修改 `/opt/liveclip/.env` 后：

```bash
cd /opt/liveclip
docker compose up -d
```

若需要 LLM 二次精排，可配置任意 OpenAI 兼容服务：

```dotenv
LLM_BASE_URL=https://your-provider.example.com/v1
LLM_MODEL=your-model
LLM_API_KEY=your-key
```

系统只向该接口发送已生成的候选片段文本，不发送完整录像。未配置、请求失败或结果格式不合法时，会自动退回本地评分，不影响出片。

## 8. API

所有 `/api/*` 请求都需要 Basic Auth。示例不把密码写在命令历史里：

```bash
read -r -p "admin username: " ADMIN_USER
read -r -s -p "admin password: " ADMIN_PASSWORD; echo
```

启动：

```bash
curl -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://live.douyin.com/123456","title":"测试直播"}' \
  http://127.0.0.1:8000/api/recordings/start
```

停止和切片：

```bash
curl -u "${ADMIN_USER}:${ADMIN_PASSWORD}" -X POST \
  http://127.0.0.1:8000/api/recordings/<任务ID>/stop

curl -u "${ADMIN_USER}:${ADMIN_PASSWORD}" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"clip_count":3,"min_seconds":20,"max_seconds":90}' \
  http://127.0.0.1:8000/api/recordings/<任务ID>/process
```

查询：

```bash
curl -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
  http://127.0.0.1:8000/api/recordings
curl -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
  http://127.0.0.1:8000/api/recordings/<任务ID>
```

## 9. 部署测试

### 9.1 容器自检

```bash
cd /opt/liveclip
docker compose exec liveclip bash /app/scripts/check_runtime.sh
```

应看到 Python、FFmpeg、yt-dlp、Faster-Whisper、编码测试和数据目录全部为 `OK`。

### 9.2 健康检查

```bash
curl -fsS http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

健康检查不需要登录，只返回固定状态，不暴露任务信息。

### 9.3 鉴权测试

错误密码应返回 `401`：

```bash
curl -i http://127.0.0.1:8000/api/recordings
```

正确密码应返回 `200`：

```bash
curl -fsS -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
  http://127.0.0.1:8000/api/recordings
```

### 9.4 端到端直播测试

1. 选一个你有权保存、正在直播且内容较短的直播间。
2. 页面录制 60–120 秒后停止。
3. 确认 `data/recordings/<ID>/recording.mkv` 存在，`ffprobe` 能读出时长：

```bash
docker compose exec liveclip \
  ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 \
  /app/data/recordings/<ID>/recording.mkv
```

4. 生成 2 条切片，检查视频是否为 1080×1920、字幕是否在安全区、音画是否同步：

```bash
docker compose exec liveclip \
  ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,codec_name \
  -of default=noprint_wrappers=1 \
  /app/data/recordings/<ID>/clips/clip_01.mp4
```

### 9.5 单元测试

在构建电脑或装有 Python 3.12 的开发机：

```bash
cd liveclip
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
python -m compileall -q app
```

## 10. 运维和故障定位

日志：

```bash
cd /opt/liveclip
docker compose logs -f --tail=200
tail -n 200 data/recordings/<ID>/ffmpeg.log
```

常见问题：

| 现象 | 排查 |
|---|---|
| 解析直播间失败 | 直播是否仍在进行；抖音链接是否过期；必要时更新 `yt-dlp`、重新导出 cookies |
| 录制很快结束且无文件 | 查看 `ffmpeg.log`，通常是流需要登录、签名 URL 过期或源站断流 |
| 卡在“正在安全停止” | `docker compose logs` 查看 FFmpeg 是否仍在收尾；不要立刻重启 |
| 首次切片很慢 | 正在下载 Whisper 模型；下载完成后会缓存在 Docker volume |
| 字幕有错字 | 把 `WHISPER_MODEL` 改为 `medium`，并重新执行切片 |
| 切片失败 | 查看 API `error` 和容器日志；`transcript.json`、`candidates.json` 会保留 |
| 磁盘不足 | 删除不再需要的 `data/recordings/<ID>`，并同步清理数据库记录或整库重建 |

更新 `yt-dlp`：

```bash
cd /opt/liveclip
docker compose exec -u root liveclip pip install -U yt-dlp
docker compose restart liveclip
```

容器重建后该临时升级会消失；长期做法是修改 `requirements.txt` 后重新构建：

```bash
docker compose build --no-cache
docker compose up -d
```

备份只需保存：

```text
.env
data/liveclip.db
data/recordings/
secrets/cookies.txt
```

## 11. 生产建议

- 立即更换在聊天中暴露过的 root 密码；配置 SSH 公钥后关闭 root 密码登录。
- 云安全组只放行必要端口；优先通过域名 HTTPS 或 VPN 访问控制台。
- 根据直播码率评估磁盘。1080p 直播常见约 2–5 GB/小时，预留至少 2–3 场直播空间。
- 单条切片先使用 20–90 秒；算法评分只是候选器，发布前必须人工复核版权、隐私、字幕和平台规则。
- 如果要提高高光准确率，优先积累“应该选/不应该选”的标注样例，再调整关键词、时长和非重叠阈值，而不只是换更大的模型。


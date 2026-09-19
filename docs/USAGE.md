# checkin-system 签到管理系统

## 飞牛 NAS Docker 部署

### 1. 上传文件到 NAS

将整个 `checkin-system` 目录上传到飞牛 NAS，例如放在 `/vol1/docker/checkin-system/`

### 2. 修改密钥

编辑 `.env` 文件，将 `CHECKIN_SECRET_KEY` 改为随机字符串：

```
CHECKIN_SECRET_KEY=你的随机密钥字符串
```

### 3. 启动容器

```bash
cd /vol1/docker/checkin-system
docker compose up -d
```

### 4. 访问管理界面

浏览器打开 `http://你的NAS_IP:5800`

---

## 使用流程

### 添加签到任务

1. 点击「添加任务」
2. 选择「福利吧论坛签到」插件
3. 填写任务名称、站点地址
4. 粘贴 Cookie（从浏览器 F12 → Application → Cookies 复制）
5. 设置定时规则，默认每天 09:00
6. 点击「创建任务」

### 如何获取 Cookie

1. 浏览器打开 https://www.wnflb2023.com 并登录
2. 按 F12 打开开发者工具
3. 点击 Application → Cookies → 选择网站域名
4. 复制所有 Cookie 的 Name=Value，用 `; ` 连接

### 导入新签到插件

1. 点击「导入插件」
2. 粘贴或上传 Python 插件代码
3. 代码需继承 `BasePlugin` 并实现 `checkin()` 方法
4. 导入后即可在添加任务时选择该插件

### HAR 通用签到（不用写代码）

**大多数站点的签到就是几个普通 HTTP 请求**，不需要为每个站写一个插件。
这类站点直接用「HAR 模板」功能：录一次抓包就够了。

1. 浏览器打开目标站点并登录 → `F12` → `Network` 面板
2. **勾选「保留日志 / Preserve log」**（不勾的话页面一跳转记录就没了）
3. 清空记录 → 手动完成一次签到操作
4. 右键请求列表 → 「另存为带内容的 HAR」→ 保存文件
5. 回到本系统 → 「HAR 模板」→ 「导入新模板」→ 上传该文件
6. 在预览里**只勾选签到相关的 1~3 个请求**（静态资源已自动取消勾选）
7. 进入编辑器：把用户名/密码/Cookie 等会变的值改成 `{{ username }}` 这样的变量
8. 填「成功断言」正则（例如 `签到成功`），可选填「失败断言」（例如 `已签到`）
9. 点「试跑一次」确认能跑通 → 保存
10. 「添加任务」→ 选「HAR 通用签到」→ 选这个模板 → 填变量 → 定时

**模板语法速查**

| 写法 | 含义 |
|------|------|
| `{{ username }}` | 变量替换（任务里填的值） |
| `{{ timestamp }}` | 内置函数：当前时间戳 |
| `{{ uuid }}` / `{{ random_num:8 }}` | 随机 UUID / 8 位随机数字 |
| `{{ md5 }}` `{{ sha256 }}` `{{ base64 }}` | 摘要与编码（等价于对同名过滤器传当前值） |
| `{{ username \| md5 }}` | 过滤器：值 `\|` 过滤器，可串联 |
| `{{ username \| substr:0:8 }}` | 过滤器带参数，参数用 `:` 分隔 |
| `{{ cookie \| get_cookie_value:'token' }}` | 从 Cookie 串里取某个键 |
| `{{ _cookies['sid'] }}` | 取当前会话 Cookie 里的值 |
| `{% if 变量 %}` … `{% else %}` … `{% endif %}` | 条件分支（写在请求的 URL 栏里） |
| `{% for item in 列表变量 %}` … `{% endif %}` | 循环（同上） |

**什么时候该写专用插件而不是用 HAR 模板**

如果签到请求带了**动态加密签名**（例如 hyperdown 的 SealJSON 信封：X25519 + HKDF +
XChaCha20-Poly1305 + HMAC），HAR 模板无法复现 —— 那种情况必须写专用插件。
判断方法：抓包看请求体/请求头里有没有一段**每次都不一样、且由页面 JS 算出来**的
签名值。有 → 写插件；没有 → HAR 模板足够。

### 通知推送（PushPlus + 微信）

PushPlus 能把签到结果直接推到微信。

1. 微信扫码关注「PushPlus 推送加」公众号（**必须关注，否则收不到**）
2. 到 https://www.pushplus.plus 登录，在「一对一推送」里复制你的 Token
3. 本系统 → 任务详情 → 关联通知 → 新建 PushPlus 通知，填 Token
4. 关联到任务，勾选「成功时推送 / 失败时推送」

**注意**：程序会检查 PushPlus 的返回码，推送失败会在任务日志里写明原因。
最常见的两种失败：
- `[903] 用户未关注公众号` —— 去微信关注「PushPlus 推送加」
- `[401] Token 无效` —— Token 复制错了

已支持的 7 个渠道都会返回可读的失败原因（不含邮件渠道）。

### 验证码识别：本地 ddddocr / 云码 jfbym

福利吧这类论坛在**新 IP 或触发风控**时会要求验证码。两种识别方式：

| 方式 | 说明 |
|---|---|
| **本地 ddddocr** | 默认。免费、不联网、无额度，识别率一般 |
| **云码 jfbym** | 识别率更高，按次计费（约 3 积分/次）。需去 https://www.jfbym.com 注册，用户中心复制 Token |
| **本地优先，失败转云码** | 先用本地（免费），本地识别不出来再调云码，兼顾成本与成功率 |

在任务表单的「验证码识别方式」里选，选云码时填 Token。
识别类型默认 `10110`（通用数英 ≤5 位），Discuz 登录验证码用这个即可。

> Cookie 直连模式不涉及验证码，只有「账号密码登录」且被挑战时才会用到。

### 程序内自动更新

**不用重新部署**（首次需做一次配置），在 Web 里就能升级。

**首次配置（一次性）**

1. 准备一个公开的 GitHub 仓库放本项目代码
2. 在项目根目录生成清单文件并提交到仓库：
   跑 `python -c "import updater,json;print(json.dumps(updater.build_manifest('版本号','说明'),ensure_ascii=False,indent=2))" > update_manifest.json`
   → 把 `update_manifest.json` 提交到仓库根目录
3. Web「系统设置」→ 填更新源 `https://github.com/你的用户名/仓库名` → 保存
4. **确认 `docker-compose.yml` 里有 `- ./:/app` 挂载**（已默认加上）。
   没有这行的话，更新写进容器的文件在重建后会丢失。

**日常升级**

1. 改完代码 → 改 `version.json` 版本号 → 重建清单 → 提交到仓库
   （顺序不能反：清单要在所有代码改完之后重建，否则哈希对不上）
2. 任意部署点：系统设置 → 「检查更新」→ 有新版就点「立即更新」
3. 更新后插件自动重载；若改的是核心模块（`app/`、`har/`），**建议重启容器**完全生效

**安全说明**

- 只允许 `github.com` 域下的更新源
- 只能覆盖代码文件，**不会碰** `data/`（数据库）、`.env`（密钥）、`user_plugins/`（你的插件）
- 每个文件都要 SHA256 校验通过才写入，任一失败整批拒绝
- 更新前自动备份到 `data/backups/<时间戳>/`，出问题可手工恢复
- 清单文件本身**没有签名**，只能防传输损坏，防不住上游仓库被篡改 —— 请确保仓库是你自己的

> 若改了 `requirements.txt`（新增依赖），仍需重建镜像安装依赖；纯代码改动才不需要。

### 手动签到

在任务列表点击「签到」按钮即可立即执行一次签到。

---

## 目录结构

```
checkin-system/
├── app/              # 后端核心代码
│   ├── main.py       # Flask 入口
│   ├── database.py   # 数据库初始化
│   ├── models.py     # 数据模型
│   ├── crypto.py     # 加密工具
│   ├── engine.py     # 签到引擎
│   ├── scheduler.py  # 定时调度
│   ├── notifier.py   # 通知推送
│   ├── plugin_loader.py  # 插件加载
│   ├── routes/       # API 路由
│   └── static/       # 静态资源
├── captcha.py        # 验证码识别统一入口（本地 ddddocr / 云码 jfbym）
├── updater.py        # 程序内自动更新（下载+校验+备份+回滚）
├── version.json      # 当前版本号
├── har/              # HAR 通用签到引擎（与 plugins 平级）
│   ├── parser.py     # HAR / cURL 解析
│   ├── render.py     # 模板渲染（变量 / 内置函数 / 过滤器链）
│   └── engine.py     # 请求链执行（断言 / 抽取 / cookie jar）
├── plugins/          # 内置插件
│   ├── base.py       # 插件基类
│   ├── fuliba.py     # 福利吧论坛
│   ├── hyperdown.py  # Hyperdown 网盘签到（账号密码登录 + SealJSON 加密信封）
│   ├── har_template.py  # HAR 通用签到（HAR 引擎的插件外壳）
│   └── _template.py  # 插件模板
├── user_plugins/     # 用户导入的插件 (volume挂载)
├── templates/        # HTML 模板
├── data/             # SQLite 数据库
├── logs/             # 日志
├── Dockerfile
├── docker-compose.yml
└── .env
```


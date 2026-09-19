# 签到管理系统 — 开发文档（Developer Guide）

> 适用对象：接手本项目继续开发的开发者、其他 AI 账号、后续维护者。
> 目标：读完本文即可在本地跑起来、理解每个模块职责、知道如何新增插件 / 通知渠道、知道历史踩过的坑。

---

## 0. 一句话定位

这是一个**轻量级、插件化、可数据库驱动扩展**的自动签到平台。为 Docker（飞牛 NAS 等）环境设计，每个"签到目标"是一个独立的 `.py` 插件文件，**丢进去就能用，不改核心代码**。内置福利吧论坛（Discuz! X3.4）签到插件，支持 **Cookie 直连** 与 **账号密码登录（含验证码自动识别，ddddocr）** 两种模式，登录成功后自动回写并加密存档 Cookie，过期时自动重新登录。

技术栈：Python 3.12 + Flask 3 + APScheduler + SQLite（WAL）+ cryptography（AES-256-CBC）+ 原生 JS/CSS 前端。

---

## 1. 目录结构与职责

```
checkin-system/
├── app/
│   ├── main.py            # Flask 入口：create_app()，初始化 DB/插件/调度器，注册蓝图
│   ├── database.py        # SQLite 连接、建表（init_db 幂等）
│   ├── crypto.py          # AES-256-CBC 加解密（密码/Cookie），密钥来自 CHECKIN_SECRET_KEY
│   ├── models.py          # 所有 DB CRUD（PluginModel/TaskModel/CheckinLogModel/NotifyModel）
│   ├── engine.py          # 签到执行引擎：组装 config → 调插件 → 存结果 → 回写 Cookie → 通知
│   ├── scheduler.py       # APScheduler 后台调度：按 Cron 加载/增删/更新任务
│   ├── notifier.py        # 多渠道通知推送（PushPlus/Server酱/Bark/钉钉/企微/TG/Webhook）
│   ├── plugin_loader.py   # 插件发现、importlib 动态加载、热导入、同步到 DB
│   ├── routes/            # 蓝图路由
│   │   ├── index.py       # 页面路由（/、/task/<id>、/task/add、/task/<id>/edit、/plugin/import）
│   │   ├── task_api.py    # /api/tasks 任务 CRUD + run/logs/calendar
│   │   ├── plugin_api.py  # /api/plugins 插件列表/schema/import/delete
│   │   ├── system_api.py  # /api/system/stats、/api/system/config
│   │   ├── notify_api.py  # /api/notify 通知配置 CRUD + 任务关联
│   │   └── har_api.py     # /api/har 模板 CRUD + 上传解析 + 试跑；页面 /har、/har/new、/har/<id>/edit
│   └── static/
│       ├── css/app.css
│       └── js/app.js      # 通用函数（toast、密码小眼睛）
├── har/                   # HAR 通用签到引擎（不写代码就能加站点的核心）
│   ├── __init__.py        # 对外入口：parse_har/parse_curl/run_entries/render/find_variables
│   ├── parser.py          # HAR 文件 / cURL 命令 → 模板条目（含响应瘦身、垃圾头剔除）
│   ├── render.py          # 模板渲染：变量替换 / 内置函数 / 过滤器链 / _cookies 取值
│   └── engine.py          # 请求链执行：断言判定 / 变量抽取 / cookie jar / if-for 控制流
├── plugins/               # 内置插件（随镜像打包，builtin=1）
│   ├── base.py            # BasePlugin 抽象基类 + FormField + CheckinResult（**不要改**）
│   ├── fuliba.py          # 福利吧论坛签到（核心参考实现，含验证码）
│   ├── hyperdown.py       # Hyperdown 网盘签到（账号密码登录 + SealJSON 加密信封）
│   ├── har_template.py    # HAR 通用签到（har/ 引擎的插件外壳）
│   └── _template.py       # 新插件模板
├── user_plugins/          # 用户导入的插件（volume 挂载，builtin=0，可热导入）
├── templates/             # Jinja2 模板：base / index / detail / task_form / plugin_import / har_*
├── data/                  # SQLite 数据库 checkin.db（volume 挂载，持久化）
├── logs/                  # 预留日志目录
├── Dockerfile             # python:3.12-slim，清华源装依赖
├── docker-compose.yml     # 端口 5800，挂载 data/logs/user_plugins，健康检查
├── .env                   # CHECKIN_SECRET_KEY（加密密钥，**生产必须改**）
├── requirements.txt       # 依赖清单（含 ddddocr）
├── README.md              # 用户向快速部署手册
└── docs/README.md         # 用户向完整手册（接口/数据库/插件标准/任务配置）
```

**挂载卷（docker-compose.yml）：**
| 宿主机 | 容器 | 用途 |
|---|---|---|
| `./data` | `/app/data` | SQLite 持久化 |
| `./logs` | `/app/logs` | 日志 |
| `./user_plugins` | `/app/user_plugins` | 用户插件 |

---

## 2. 启动流程（create_app 做了什么）

`app/main.py` 的 `create_app()` 顺序：

1. `init_db()` — 建表（幂等，`CREATE TABLE IF NOT EXISTS`）。
2. `load_all_plugins()` — 扫描 `plugins/`（内置）与 `user_plugins/`（用户），import 插件类，写入 `plugins` 表（见 §5）。
3. `start()` — 启动 APScheduler 后台调度器。
4. `load_all_tasks()` — 把所有 `enabled=1` 的任务按各自 Cron 加到调度器。
5. 注册 6 个蓝图（index / tasks / plugins / notify / system / har）。注意 har 蓝图**不加 url_prefix**，因为它的路径写全了（`/har` 页面 + `/api/har` 接口）。
6. `atexit.register(shutdown)` — 进程退出时停调度器。

本地运行：`python -m app.main`（需把项目根目录作为 cwd，因为 `sys.path.insert(0, 项目根)` 在 main.py 里做了）。

---

## 3. 数据流（一次签到从触发到落库）

```
触发器（手动 POST /api/tasks/<id>/run  或  调度器 Cron 到点）
        │
        ▼
engine.execute_checkin(task_id)
        │
        ├─ TaskModel.get_by_id()  → 解密 password/cookie，反序列化 params/last_extra
        ├─ get_plugin(plugin_display_name 或 plugin.name)
        ├─ 组装 config = {username, password, cookie, site_url, params:{...}}
        │
        ├─ plugin.pre_checkin(config)  → 返回非 None 则提前终止
        ├─ result = plugin.checkin(config)   ★ 插件核心逻辑
        ├─ result = plugin.post_checkin(config, result)
        │
        ├─ if result.cookie:  TaskModel.update_cookie()  ← 登录类插件回写最新 Cookie（加密）
        ├─ TaskModel.update_run_result()  ← 写 tasks 状态 + checkin_logs（同日去重）
        └─ send_notification()  ← 按任务关联的通知渠道推送
```

关键约定（务必遵守，否则续作者会踩坑）：

- **插件永远只通过 `config` 拿数据**：`config["cookie"]`、`config["username"]`、`config["password"]`、`config["site_url"]`、`config["params"]`。这些值在 models.py 读取时已解密，插件里拿到的是明文。
- **通用字段（username/password/cookie/site_url）不要放进插件 form_schema**，它们由任务表单顶层提供。插件 `form_schema` 只声明**专属参数**（如 retry）。前端渲染时会跳过这 4 个 key（见 §8）。
- **Cookie 回写机制**：若插件登录成功后把最新 Cookie 赋给 `result.cookie`，engine 会自动加密存档；下次执行直接用最新 Cookie，实现"登录一次、自动续期"。详见 §6 福利吧实现。
- **统计去重**：`/api/system/stats` 用 `COUNT(DISTINCT task_id)`；`update_run_result` 对同一天同一任务先查后插/更新（不会重复累加）。**这两个去重是踩过坑后加的**，不要去掉。

---

## 4. 数据库（SQLite，WAL）

建表语句见 `app/database.py` 的 `init_db()`，共 7 张表：

| 表 | 作用 |
|---|---|
| `plugins` | 插件注册表。form_schema 是 JSON 数组（字段定义）。builtin=1 为内置。 |
| `tasks` | 签到任务。password/cookie 存**密文**；params 存任意 JSON 扩展参数（设计原则：永不够用，所有扩展走 params）。 |
| `checkin_logs` | 每日签到记录。索引 `(task_id, checkin_date)`。**同日同任务唯一**（去重更新）。 |
| `notify_configs` | 通知渠道配置。config 是 JSON。 |
| `task_notify` | 任务 ↔ 通知 多对多关联，含 on_success/on_failure。 |
| `system_config` | 键值配置。 |
| `har_templates` | HAR 通用签到模板。entries 是 JSON（请求定义+断言+抽取规则整体存），variables 是自动识别的变量名列表。 |

**核心设计原则（续作者必须遵守）：**

> **永远不新增 tasks 表字段。** 任何新参数都放进 `params` 这个 JSON 字段。新增"签到类型"靠插件，不是靠改表结构。这是系统可无限扩展的根。

常用字段含义见 `docs/README.md` 第 3 章（用户手册），那里表格更全。开发侧需特别留意：

- `TaskModel.get_by_id / get_all` 读取时会**自动 decrypt(password/cookie)** 并 `json.loads(params/last_extra)`。**写入时**（create/update）会 `encrypt()`。不要在插件或 engine 里再 decrypt 一次，会重复。
- `update_run_result(task_id, result, message, extra, duration_ms, error_trace)`：
  - 更新 tasks 的 last_*、status（success→normal / failed→error）、consecutive_fail（成功归零，失败 +1）、status_message（仅失败时写）。
  - 同时 upsert `checkin_logs`（同日唯一）。
  - extra 必须是 dict（会被 `json.dumps`）。
- `update_cookie(task_id, cookie)`：把登录拿到的最新 Cookie 加密存回 tasks.cookie。

---

## 5. 插件加载机制（plugin_loader.py）

- **发现**：`_load_from_dir` 遍历目录，跳过以 `_` 开头的文件（所以模板叫 `_template.py` 不会被加载），对每个 `.py` 用 `importlib.util.spec_from_file_location` 加载。
- **识别插件类**：`_discover_plugin_classes` 从模块里找出所有 `BasePlugin` 的子类（且不是 BasePlugin 本身）。**一个文件可以定义多个插件类**。
- **—sync_to_db**：把插件元信息（name/display_name/.../form_schema）写入 `plugins` 表，已存在则 UPDATE（不重建，保留 enabled 等状态）。
- **内存表**：`_loaded_plugins: dict`（name → 实例），供 `get_plugin(name)` / `get_all_plugins()` 查询。engine 通过 `get_plugin` 取实例。
- **热导入**：`load_user_plugin(source_code, filename)` 把代码写到 `user_plugins/`，import 校验，写入内存表 + DB。重复文件名直接拒绝。校验失败（无 BasePlugin 子类 / 缺 name）会删掉刚写的文件。
- **删除**：`delete_user_plugin` 删文件 + 从内存表移除 + 删 DB 行（仅非内置）。

> 注入路径：`sys.path.insert(0, os.path.dirname(directory))`，保证插件内部 `from plugins.base import ...` 可解析（plugins 与 user_plugins 同级，根目录在 sys.path）。

---

## 6. 插件标准（开发者必须读 §5.2 的"基类接口"版，这里只讲续作要点）

基类定义见 `plugins/base.py`：

```python
class BasePlugin(ABC):
    name: str                       # 唯一标识，如 "fuliba"
    display_name: str
    description: str
    version: str = "1.0"
    plugin_type: str = "http"       # http/selenium/playwright/api/custom
    author: str
    form_schema: List[FormField] = []

    def checkin(self, task_config) -> CheckinResult: ...   # 必须实现
    def pre_checkin(self, c) -> Optional[CheckinResult]: return None   # 可选，提前终止
    def post_checkin(self, c, result) -> result: return result          # 可选，改结果
```

```python
@dataclass
class CheckinResult:
    success: bool
    message: str
    extra: dict = {}            # 任意附加信息（天数、积分等），前端日历/通知会展示
    cookie: str = ""            # ★ 登录类插件签到成功后回填最新 Cookie，engine 会加密存档
```

`FormField(key, label, type, required, default, placeholder, options, help_text, sensitive)`，type 支持 text/password/url/textarea/select/checkbox/number；`sensitive=True` 的字段前端带小眼睛、存储加密。

**新增插件的最小步骤（完整示例见 `plugins/_template.py` 和 `docs/README.md` 第 5.3 节）：**
1. 复制 `plugins/_template.py` → `plugins/your.py`（内置）或交给用户在 Web「导入插件」上传（用户插件）。
2. 改 `name` / `display_name` / `form_schema`。
3. 实现 `checkin(config)`，最后 `return CheckinResult(...)`。
4. 重启（内置）或 Web 导入（用户）。前端会自动渲染表单。

---

## 7. 福利吧插件详解（fuliba.py — 核心参考实现，也是最难的部分）

论坛基于 **Discuz! X3.4**，`fx_checkin` 签到插件。页面 **GBK 编码**，需兼容解码。

### 7.1 两种认证模式

`checkin()` 主逻辑（fuliba.py L444）：

1. **Cookie 直连**：若任务填了 cookie，先 `session.cookies.update` 后直接 `_do_checkin`。
   - 若签到成功 → 把最新 session Cookie 赋给 `result.cookie`（回写存档）。
   - 若返回 `cookie_expired` 且同时填了账号密码 → **自动回退到账号密码登录流程**。
2. **账号密码登录**：`_do_login`（含验证码挑战）→ 登录成功后再 `_do_checkin`，成功同样回写 Cookie。

> 设计意图：用户只需填一次账号密码，之后系统自动维护 Cookie，过期自动重登（带验证码识别），用户无感知。

### 7.2 登录态判断（最可靠信号）

`_check_logged_in(html)`（L60）：正则抓页面 JS 里的 `discuz_uid = '数字'`，**非 '0' 即已登录**。这是比"是否含某个 DOM"更可靠的判定，续作者别换。

### 7.3 签到请求

`_do_checkin`（L389）：
- 访问首页（forum.php）拿 `fx_checkin:checkin&formhash=A&B` 两个 hash（`_extract_formhash_pair`）。
- GET `plugin.php?id=fx_checkin:checkin&formhash=A&B&inajax=1`。
- 结果在 XML/CDATA 里，用 `_extract_message`（L478，模块级纯函数）剥离标签取中文。
- 关键文案判定：`签到成功` / `已经签到|已签到|无需重复签到` / `请登录|先登录`（→ cookie_expired）。

### 7.4 验证码自动识别（与 fmdxx1991/wnflb-checkin 仓库对齐的能力）

触发条件：新 IP、风控、短时间内多次登录会要求验证码。

`_do_login`（L281）流程：
1. GET 登录页，解析 `formhash` + `loginhash`。
2. 首次无验证码 POST 提交（老 IP 通常直接成功）。
3. 被挑战（响应里带 `auth=` 和验证码输入框）→ 进入验证码分支：
   - `_extract_login_fields` 从挑战页抓 formhash/loginhash/auth（**auth 可能以 `%2F` 形式含 `/`，必须 `urllib.parse.unquote` 还原**）。
   - `_detect_captcha` 抓 `idhash`/`seccodehash`/`auth`/`update`（多个正则兜底，见 L85）。
   - `_solve_captcha`（L153）：**懒加载 `import ddddocr`**（只在需要时才 import，避免常态加载 onnxruntime 拖慢 Cookie 模式）；拉 `misc.php?mod=seccode&update=...&idhash=...` 图片，校验 PNG/JPEG 头，用 `ocr.classification` 识别；**最多 CAPTCHA_ATTEMPTS=3 次换图重试**。
   - `_verify_captcha_code`：调 `misc.php?mod=seccode&action=check&secverify=...` 校验识别是否正确（在 cookie 写入 seccode 标记）。
   - `_submit_login(challenge=True)`：二次提交 `auth` + `seccodeverify`，**不带账号密码**（凭据已由 auth 关联）。
   - 验证码不正确 → 换新图重试；凭据失效（auth 过期）→ 重拉挑战页拿新 auth 重试一次。

### 7.5 关键坑（续作者务必知道）

- **GBK 解码**：Discuz 默认 GBK，`resp.text` 可能乱码。统一用 `_decode(resp)`（L35）优先 GBK。新插件若遇到乱码，先检查编码。
- **`_extract_message` 必须是模块级函数**，不要写成类方法（历史踩坑：曾误写为方法导致登录/签到调用不到）。它同时要被登录与签到共用。
- **`auth` 的 unquote**：HTML 里 auth 值可能 URL 编码，`%2F` = `/`，不还原会导致二次提交失败。
- **ddddocr 懒加载**：不要在模块顶部 `import ddddocr`，否则镜像常态加载 onnxruntime（体积/启动代价大）。仅在 `_solve_captcha` 内 import。
- **`_check_logged_in` / `_extract_formhash_pair` 是模块级函数**，在 `_do_checkin`、`_submit_login` 里以**函数名直接调用**，不要加 `self.`。

---

## 8. 前端（模板 + JS）

- **布局**：`templates/base.html` 提供外壳（含 toast、密码小眼睛 CSS、统计卡片样式）。各页继承。
- **任务列表** `index.html`：表格 + 顶部统计卡（AJAX 拉 `/api/system/stats`）。密码列带小眼睛（readonly input + 切换按钮）。表格 `min-width:1100px` 横向滚动。
- **任务表单** `task_form.html` + 内联 JS：
  - 选插件 → `fetch /api/plugins/<id>/schema` → `renderDynamicFields` 动态渲染专属参数。
  - **跳过通用字段**：`const commonFields = ['cookie','username','password','site_url']`，这些已在表单顶层，专属区不重复。
  - 提交时：收集所有 `param_*` 为 `params`，并把顶层 cookie 同步进 `params.cookie`（确保插件能读到）。
  - 表单提交走 `fetch('/api/tasks', POST)` 或 `PUT /api/tasks/<id>`。
- **详情页** `detail.html`：签到日历（GitHub 风格热力图，固定紧凑布局）+ 记录列表。日历数据来自 `/api/tasks/<id>/calendar?year=&month=`，前端按 `checkin_date → result` 着色（success/failed/missed/future）。
- **公共 JS** `app/static/js/app.js`：`showToast` / `togglePassword` / `togglePasswordBtn`（小眼睛核心）。

> 续作者改 UI 时：样式在 `app/static/css/app.css`；日历格子宽度已在历史中定为固定紧凑（约 36px 格、280px 宽），不要改回自适应导致的右侧大留白。

---

## 9. 通知系统（notifier.py）

7 种渠道：`pushplus` / `serverchan` / `bark` / `webhook` / `dingtalk`（含加签）/ `wecom` / `telegram`。

`send_notification(task_name, result, message, extra, task_id)`：
- 取该任务关联的启用通知（`NotifyModel.get_for_task`）。
- 按 `on_success` / `on_failure` 决定是否推送。
- 各渠道私有 `_send_xxx` 函数。**新增渠道步骤**：(1) 在 notifier.py 加 `_send_xxx`；(2) 在 `send_notification` 的 if/elif 链里加分支；(3) 配置格式约定见 `docs/README.md` 第 3.1 节 notify_configs.config 示例；(4) （可选）前端若需专属配置 UI 再改表单。

---

## 10. 本地开发与测试

### 10.1 环境准备

镜像基于 `python:3.12-slim`。本地开发建议同版本 Python 3.12 + venv：

```bash
cd checkin-system
python -m venv venv
venv\Scripts\activate        # Windows；Linux/Mac 用 source venv/bin/activate
pip install -r requirements.txt
```

> 注意：`ddddocr` 会拉 `onnxruntime`，体积较大（约几百 MB），安装较慢属正常。若只测 Cookie 模式可不装，但 `_solve_captcha` 会因 ImportError 优雅降级（返回"未安装 ddddocr"）。

### 10.2 启动

```bash
# 项目根目录为 cwd
set CHECKIN_SECRET_KEY=dev-only-key
python -m app.main
# 访问 http://127.0.0.1:5800
```

### 10.3 离线测试（无需网络/容器）

核心逻辑可用 Flask `test_client` + SQLite 内存库离线验证（历史验证均如此）。示例骨架：

```python
import sqlite3, tempfile
from app.main import create_app
# 用临时 DB 避免污染 data/checkin.db
app = create_app()
client = app.test_client()
# 1) 插件是否正常加载
r = client.get("/api/plugins")
assert any(p["name"] == "fuliba" for p in r.get_json())
# 2) formhash 正则是否仍有效（针对真实首页 HTML 片段）
from plugins.fuliba import _extract_formhash_pair, _check_logged_in
assert _extract_formhash_pair(html)  # 防止论坛改版后静默失效
```

> 续作者提醒：福利吧论坛结构（formhash 正则、`discuz_uid` 判定、签到文案）若论坛改版会**静默失效**。改版时优先用 `_extract_formhash_pair` / `_extract_message` 的单元测试回放真实 HTML 验证。

### 10.4 验证清单（交付前自测）

- [ ] `load_all_plugins` 后 `/api/plugins` 含 fuliba。
- [ ] 用账号密码模式跑一次真实签到（或 mock 挑战页验证登录链路）。
- [ ] `CheckinResult.cookie` 回写后，`tasks.cookie` 为密文且下次复用。
- [ ] 验证码流程：拉图→识别→校验→二次提交→判断 `discuz_uid!=0`。
- [ ] `stats` 去重、同日签到不重复累加。
- [ ] 内置插件改代码需重启容器；用户插件 Web 导入即时生效。

---

## 11. 构建与部署（Docker）

`docker-compose.yml`：服务名 `checkin`，端口 `5800:5800`，健康检查每 60s 探 `/`。

```bash
# 1) 改密钥（重要！否则密码/Cookie 用默认弱密钥加密）
#    编辑 .env 的 CHECKIN_SECRET_KEY 为随机串
# 2) 构建并后台启动
docker compose up -d --build
# 3) 查看
docker compose ps
docker compose logs -f
```

- 镜像体积：含 `ddddocr`（onnxruntime）后约 **300MB+**，不再是早期 ~65MB。规划磁盘时按 350MB 预估。
- 升级代码：改完直接 `docker compose up -d --build`（旧文档写过的 65MB 已过时）。
- 端口冲突：改 `docker-compose.yml` 左侧端口（如 `5801:5800`）。
- 备份：`cp -r data/ <备份路径>`（DB + 用户插件在挂载卷，但 user_plugins 是独立卷需单独备份）。

---

## 12. 配置项

| 变量 | 位置 | 说明 |
|---|---|---|
| `CHECKIN_SECRET_KEY` | `.env` / 容器环境变量 | AES 密钥（由 `crypto.py` 做 SHA-256 派生）。**生产必须改随机串**。改了之后旧的加密密码/Cookie 无法解密（需重新填）。 |
| `PORT` | 环境变量（默认 5800） | Flask 监听端口 |
| `TZ` | docker-compose（Asia/Shanghai） | 调度器时区固定 `Asia/Shanghai`（`scheduler.py` 硬编码），TZ 仅影响日志时间戳 |

---

## 13. 历史踩坑与续作禁忌（最重要的一节）

以下均为真实修过的 bug，**改代码前先读**：

1. **插件加载 importlib 导入问题**：曾因 import 路径/命名空间导致插件类识别失败。现在 `_load_from_dir` 用 `spec_from_file_location(f"plugins.{mod_name}", full_path)`，并 `sys.path.insert(0, dirname)`，保证 `from plugins.base import` 可解析。**不要改成普通 import**。
2. **统计重复累加**：`today_success` 曾用 `COUNT(*)` 重复计。已改为 `COUNT(DISTINCT task_id)`；`update_run_result` 同日 upsert。**不要改回 COUNT(*)**。
3. **表单双 Cookie**：早期专属参数里也渲染了 cookie，造成"通用 Cookie"与"专属 cookie"两个框。现在前端 `commonFields` 跳过 4 个通用 key，提交时通用 Cookie 同步进 `params.cookie`。**新增通用字段也要加进 commonFields**。
4. **`_check_logged_in` / `_extract_formhash_pair` 误用 `self.` 调用**：这两个是模块级函数，曾误写成类方法导致调用失败。现均为模块级，调用时直接写函数名。
5. **`_extract_message` 误写为类方法**：登录与签到都要用，提取纯中文消息。现为模块级函数（L478）。
6. **登录页 mock 漏 `loginhash`**：测试用 mock 挑战页必须含 `loginhash=xxx`，否则 `_extract_login_fields` 取不到。真实页面有，仅测试数据需注意。
7. **`auth` 未 `unquote`**：验证码二次提交前必须对 auth 做 `urllib.parse.unquote`，否则 `/` 被编码成 `%2F` 导致失败。
8. **ddddocr 不要顶层 import**：懒加载在 `_solve_captcha` 内，避免常态加载 onnxruntime。
9. **GBK 解码**：Discuz 默认 GBK，所有 `resp.text` 走 `_decode`，新插件遇到乱码先查编码。
10. **日历布局**：曾改为自适应导致右侧大留白/格子过大。现为固定紧凑（36px 格、280px 宽），改动需谨慎。

---

---

## 14. HAR 通用签到引擎（har/ 包）

### 14.1 为什么有这个包

项目原来的扩展方式是「每个站点写一个 `.py` 插件」。但绝大多数站点的签到就是
**几个普通 HTTP 请求**，为它们逐个写插件是浪费 —— 你要为每个站点重新实现一遍
登录、取 formhash、提交签到、判断成败。

`har/` 把这件事抽象成「录一次抓包 → 在界面上配置 → 定时跑」：
用户不需要写任何代码，开发者也不需要为每个站点写插件。
**专用插件仍然保留**，用来处理 har 引擎覆盖不了的场景（见 16.6）。

### 14.2 三个模块的职责

```
har/parser.py   HAR文件/cURL  ──解析──►  模板条目(entries)
har/render.py   模板条目 + 变量  ──渲染──►  真实请求(method/url/headers/body)
har/engine.py   真实请求  ──执行──►  结果 + 抽取的变量 + cookie jar
```

**parser.py** 关键取舍：
- 只保留**请求**信息，响应体一律丢弃。一个真实 HAR 动辄几十 MB（含所有响应、
  图片 base64），剥掉响应后通常只剩几 KB —— 存进 SQLite 才不会撑爆。
- 自动剔除会捣乱的请求头：`content-length`/`host`/`connection`（requests 自己会生成，
  写死会出错）、`sec-ch-ua*`/`sec-fetch-*`/`accept-encoding`（浏览器指纹，requests
  行为对不上反而容易触发风控）。名单在 `DROP_HEADERS`。
- 静态资源（`.js/.css/图片`）默认 `checked: False`，用户不用手动一个个取消。
- `request.cookies` 与 `Cookie` 头**合并到 cookies 字段**，不放进 headers。

**render.py** 支持的语法（判定顺序不可变）：
1. 管道链 `{{ a | f1 | f2:arg }}` —— 先按顶层 `|` 切开，逐段套用
2. 函数调用 `{{ timestamp }}` / `{{ random_num:8 }}`
3. 变量 / 属性取值 `{{ username }}` / `{{ _cookies['sid'] }}` / `{{ resp.data.token }}`
4. Jinja2 兜底（算术、字符串拼接）

> ⚠️ **判定顺序铁律：用户变量 > 内置函数**。`md5` 既是内置函数名又是过滤器名，
> 若用户恰好定义了名为 `md5` 的变量，必须让变量优先，否则会渲染出
> `<function _md5 at 0x...>`。这个坑已踩过一次。

**engine.py** 关键行为：
- 控制流写在**请求的 url 字段**里（`url: "{% if x %}"`），由 `_expand_control_flow`
  编译成嵌套结构。这跟 QD 一致，好处是不用给 entries 加额外字段。
- 断言 `success_asserts` / `failed_asserts` 命中规则：**成功断言全不中 → 失败；
  失败断言任一中 → 失败**。
- `__log__` 是**哨兵不是正则**：命中它表示"把整个响应体存进 `__log__` 变量"，
  本身不参与成败判定。必须先从断言列表里摘掉，否则正则 `__log__` 永远匹配不上
  正常响应体，会把每个请求都判成失败（这个 bug 已修）。
- cookie 合并顺序：**先放模板自带的旧 cookie，再用 env.cookies（现存会话）覆盖**。
  反过来会导致"回写复用的 Cookie 永远不生效"。

### 14.3 数据在库里怎么存

`har_templates` 表：
```
entries    TEXT  -- JSON：整份模板（请求定义 + 断言 + 抽取规则）
variables  TEXT  -- JSON：自动识别出的变量名列表
```
**不把每个请求拆成一行**：模板是一个整体，整体读整体写更简单；几十个请求也就
几十 KB，SQLite 完全扛得住。

`HarTemplateModel` 出库时**自动 json.loads**，调用方拿到的是已解析的 list/dict，
不要重复 loads。

任务侧：模板 id 和变量值都放进 `tasks.params`（JSON），遵守项目铁律「永不加表字段」：
```json
{"template_id": 3, "variables": {"username": "...", "site_url": "..."}, "timeout": 30}
```

### 14.4 变量自动识别

`find_variables(entries)` 扫描所有请求的 method/url/headers/cookies/body，
把 `{{ }}` 里出现的名字收集起来，**排除内置函数/过滤器名**（`RESERVED_NAMES`）
和 `_cookies`/`_variables`，只取管道链**最左边**那一节（右边都是过滤器名）。

编辑器保存时会调 `/api/har/variables` 重新推断，同时**保留用户手动加的变量**
（推断结果里没有、但当前列表里有的，追加到后面）。

### 14.5 前端三个页面

| 页面 | 路由 | 作用 |
|---|---|---|
| 模板列表 | `/har` | 列出所有模板，显示请求数/变量/被多少任务在用 |
| 导入 | `/har/new` | 上传 HAR 或粘 cURL → 解析预览 → 勾选请求 → 建模板 |
| 编辑器 | `/har/<id>/edit` | 逐请求改 method/url/headers/body、填断言与抽取、填变量默认值、试跑 |

> ⚠️ `har_editor.html` 里整段 JS 被 `{% raw %}` 包住 —— 因为那块代码到处是
> `{{ }}` 的字面示例（提示文案），不 raw 的话 Jinja2 会当自己的表达式解析，
> 轻则渲染错乱、重则 `TemplateSyntaxError` 直接 500。
> **代价是数据不能靠 Jinja 插值传进去**，改为服务端对 `__TPL_ID__` /
> `__ENTRIES__` / `__VARIABLES__` 做字符串替换（见 `har_api.har_edit`）。
> 注入 JSON 时要把 `</` 转义成 `<\/`，否则字符串里的 `</script>` 会提前闭合标签。

### 14.6 HAR 模板 vs 专用插件：怎么选

判断标准就一条：**请求里有没有"每次都不一样、由页面 JS 算出来"的签名**。

| 情况 | 方案 | 例子 |
|---|---|---|
| 普通 Cookie / 表单提交 | HAR 模板 | 大多数 Discuz 论坛、简单 API 签到 |
| 动态加密签名 | 专用插件 | hyperdown 的 SealJSON（X25519+HKDF+XChaCha20+HMAC） |
| 需要验证码 OCR | 专用插件 | 福利吧论坛（ddddocr） |
| 需要浏览器渲染 | 专用插件 | 纯 JS 挑战（Cloudflare 5s 盾等） |

签名类拦截靠 HAR 模板**做不到**（模板只能做变量替换和简单编码，无法复现
非对称加密协商），别硬试，直接写插件。

### 14.7 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/har/upload` | 解析 HAR/cURL，返回条目（不落库） |
| GET/POST | `/api/har` | 列表 / 新建 |
| GET/PUT/DELETE | `/api/har/<id>` | 详情 / 保存 / 删除（被任务占用时拒绝删） |
| POST | `/api/har/<id>/test` | 用给定变量**真发请求**试跑 |
| GET | `/api/har/<id>/export` | 导出为 .har 文件 |
| POST | `/api/har/variables` | 重新推断变量名 |

### 14.8 har 引擎踩坑记录

1. **`har.render` 被同名函数遮蔽**：`__init__.py` 里 `from .render import render`
   会让包的 `har.render` 属性变成**函数**而不是**子模块**，之后
   `from har.render import RESERVED_NAMES` 会拿到函数并报 AttributeError。
   现在改成 `from .render import render as _render_template` 再赋值，绕开遮蔽。
2. **过滤器链是死代码**：最初的 `_FILTER_CHAIN_RE` 只匹配 `name:args`，永远匹配不上
   `name | filter`，导致所有 `|` 写法静默失效（渲染出空串或原值）。现在
   `_split_head_and_pipes` 先按顶层 `|` 切（注意跳过 `||`、括号内、引号内），
   再逐段套用。
3. **`{{ timestamp }}` 渲染成 `<function ...>`**：因为无参调用走到了"变量查找"分支。
   修正为「名字在 ALL_CALLABLES 里且不在用户变量表里 → 当函数调用」。
4. **模板编辑页 500**：JS 里的 `{{ }}` 字面示例被 Jinja2 解析。用 `{% raw %}` 整段包住。
5. **`__log__` 把每个请求都判成失败**：见 16.2。
6. **归档 Cookie 不生效**：合并顺序反了，见 16.2。
7. **`requests` 走本机代理**：本机若有 `HTTP_PROXY` 环境变量，测试用假域名
   （如 `demo.test`）会被当外网走代理而失败。测试里用 `127.0.0.1:端口` 规避。

### 14.9 顺手修掉的两个老 bug（与 har 无关，但影响全局）

1. **`engine.py` 插件查找顺序**：原来先拿 `plugin_display_name`（中文显示名，如
   `"HAR 通用签到"`）去 `get_plugin()` 查，这个值**永远不在** `_loaded_plugins`
   的键里（键是 `name`），每次都靠后面 `plugin_id` 兜底才成功。
   已改为先按 `plugin_id` 查 `name`，再用 display_name 兜底。
2. **任务删不掉**：`TaskModel.delete` 直接 `DELETE FROM tasks`，而
   `checkin_logs.task_id` / `task_notify.task_id` 的外键**没写 ON DELETE CASCADE**，
   加上 `PRAGMA foreign_keys=ON`，导致**只要任务跑过一次就再也删不掉**
   （接口只回 500，界面看不到原因）。已改为先删子表再删主表。

### 14.10 测试

`__har_test/` 下两套（都是纯本地、mock HTTP，不打真实站点）：

```bash
# 57 项：渲染引擎 / HAR 解析 / 执行引擎（含控制流、cookie、异常路径）
python __har_test/selftest.py

# 41 项：Flask 端到端（上传→建模板→建任务→执行→断言失败→删除保护）
python __har_test/integration.py
```

集成测试用**临时数据库**（在 import `app.database` 之前改 `DB_DIR`/`DB_PATH`），
不会污染 `data/checkin.db`。

---

## 15. 常见续作任务清单

| 任务 | 改哪里 |
|---|---|
| 新增一个论坛/站点签到（普通 HTTP） | **优先用 HAR 模板**（Web「HAR 模板」→ 导入抓包），无需写代码 |
| 新增一个站点签到（有加密签名/验证码） | 新建 `plugins/xxx.py` 继承 BasePlugin，或 Web「导入插件」上传 |
| 改 HAR 引擎能力（新过滤器/新内置函数） | `har/render.py` 的 `BUILTIN_FUNCS` / `FILTERS` 字典 |
| 新增通知渠道 | `app/notifier.py` 加 `_send_xxx`（**必须返回 (ok, detail) 并检查响应体**）+ `_dispatch` 分支 |
| 换验证码识别服务 | `captcha.py` 加一个 `solve_xxx`，在 `solve()` 的 order 里接入 |
| 调整更新白名单 | `updater.py` 的 `ALLOWED_HOSTS` / `ALLOWED_DIRS` / `ALLOWED_ROOT_FILES` |
| 改定时默认/调度时区 | `app/scheduler.py`（时区硬编码 Asia/Shanghai） |
| 改前端样式 | `app/static/css/app.css` |
| 改表单/动态字段渲染 | `templates/task_form.html` 内联 JS |
| 改数据库表 | `app/database.py` 的 init_db（**优先用 params 扩展，别加字段**） |
| 改加密强度/密钥派生 | `app/crypto.py` |
| 改部署/镜像 | `Dockerfile` / `docker-compose.yml` / `requirements.txt` |

---

## 16. 通知推送、验证码识别、程序内更新

### 16.1 通知推送（app/notifier.py）

**核心约定：每个渠道都必须检查响应体，不能只看 HTTP 状态码。**
这些服务的 HTTP 状态码普遍是 200，真实结果藏在响应 JSON 的 code 字段里。
历史上 `_send_pushplus` 只用 `requests.post(...)` 不读响应，导致
「Token 无效」「未关注公众号」被静默当成发送成功 —— 用户以为收到了，其实没有。

现在的实现：

- 每个 `_send_xxx` 返回 `(ok: bool, detail: str)`，detail 是**人话**。
- `send_notification()` 返回 `[{channel, ok, detail}, ...]`；
  `engine.execute_checkin` 会把失败渠道拼进 `result.message` 和
  `result.extra["notify_error"]`，任务日志里直接可见。
- PushPlus 成功判定是 **`code == 200`**，错误码已翻译（见 `PUSHPLUS_ERRORS`）：
  - `903` = 用户未关注「PushPlus 推送加」公众号 —— **最常见**的坑
  - `401/403` = Token 无效/已禁用
  - `904` = 当日推送上限
- 通知正文里**排除 `logs` 字段**（HAR 执行日志可能几千字，塞进推送会被截断或超长）。

### 16.2 验证码识别（captcha.py）

新增 `captcha.py` 作为统一入口，插件不再直接依赖 ddddocr。

```python
from captcha import solve, CaptchaError
code, used = solve(image_bytes, backend="cloud", token="...", type_id="10110")
```

- `backend`：`local`（本地）/ `cloud`（云码）/ `auto`（本地优先，失败转云码）/
  **空串 `""` = 跟随系统设置的全局默认**（推荐，见 §16.3）
- **ddddocr 懒加载 + 单例缓存**：不要改回顶层 import（会拉起 onnxruntime 几百 MB，
  而多数任务用 Cookie 直连根本不需要验证码）。单例是为了避免每次识别重新初始化模型。
- 云码 API（2026-09 核对官方文档）：
  `POST http://api.jfbym.com/api/YmServer/customApi`，
  body `{token, type, image(标准base64，无 data: 前缀)}`，
  **成功判定是外层 `code == 10000`，结果在 `data.data`**。
  ⚠️ 外层 code 与 data.code 是两个东西，别混。常见码：
  10001 参数错 / 10002 余额不足 / 10003 Token 无效 / 10004 类型不支持。
  这些错误码**必须翻译成人话**再抛给用户（`JFBYM_ERRORS`）。
- 参数类错误（Token 错、余额不足、类型不支持）**不重试** —— 重试只是浪费钱和时间，
  直接上抛让用户去改配置。
- `data` 字段兼容 dict 与 list 两种返回形态。
- **异常必须 catch 宽**：`_get_local_ocr()` 里 import ddddocr 只 catch `ImportError`
  是不够的 —— 组件包装了但跑不起来（缺 .so、模型不全、onnx 版本不匹配）会抛
  `AttributeError` / `OSError` 等，必须一并翻译成人话，否则任务直接崩。

插件侧（fuliba）有 3 个表单字段：`ocr_backend` / `jfbym_token` / `jfbym_type`，
`_solve_captcha(session, cap, ocr_conf)` 多接一个配置参数。
**三者的默认值都是空**，表示"跟随系统设置的全局配置"，插件不必重复配一遍。
`jfbym_token` / `jfbym_type` 为空时由 `captcha.solve()` 自动回退全局值。

### 16.3 可选组件：本地验证码识别包（app/extras.py）

**为什么有这个东西**：主安装包从 314MB 砍到 37MB，代价是把本地识别依赖
（ddddocr + OpenCV + ONNX + NumPy，解包约 390MB）从包里拿掉了。为了不牺牲功能，
把它做成**按需上传的可选组件**。

**目录约定（关键设计）**

```
<CHECKIN_DATA_DIR>/extras/captcha-local/
    site-packages/          ← 组件内容，会被 insert(0) 进 sys.path
    installed_manifest.json ← 安装时存入的 manifest
    .enabled                ← 存在=启用
```

放在**数据目录**里是故意的：fpk 模式下 `CHECKIN_DATA_DIR=$TRIM_PKGVAR`（@appdata），
**升级/重装应用不会丢**，用户不必每次升级都重传 390MB。

**启动时注入**：`create_app()` 里在 `load_all_plugins()` **之前**调
`extras.apply_all()`，否则插件 import captcha 时找不到用户上传的库。
顺序错了插件会 import 失败。

**captcha 侧配合**：`captcha._ensure_local_importable()` 每次本地识别前调
`extras.apply_to_syspath()`，保证独立脚本/新进程也能找到组件。

**接口**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/extras/status` | 组件状态 + 全局识别设置 |
| POST | `/api/extras/upload` | 上传组件包（multipart `file`，或 JSON base64） |
| POST | `/api/extras/toggle` | 启用/停用 `{"enabled":bool}` |
| DELETE | `/api/extras/captcha-local` | 卸载 |
| POST | `/api/extras/settings` | 保存全局识别方式 / 云码 Token |
| POST | `/api/extras/test-local` | 用内置测试图验证本地识别真能跑 |

**⚠️ 安全模型（必读）**

这个功能允许上传并 import 任意 Python 包，等价于**远程代码执行**能力。
已做的限制：

1. 只收 zip，且必须含 `manifest.json` 且 `name == "captcha-local"`
2. **防 zip slip**：拒绝 `..` / 绝对路径 / 盘符 / 反斜杠穿越的成员，
   解压后又用 `realpath` 二次确认落在目标目录内
3. 只解压到 `extras/` 下，不碰代码目录
4. 大小上限（zip 800MB / 解压后 2GB）
5. 必须先安装才能启用；未装时不允许把识别方式设成"仅本地"

⚠️ 知情即可：这些**防不住用户自己上传恶意包**（那是他自己的机器，本就有此权限）。
真正的信任边界是"谁能访问这个 Web 后台"。

**组件包格式**

```
captcha-local-pack-1.0.zip
  manifest.json      {"name":"captcha-local","version":"1.0","python":"3.12",
                      "platform":"linux_x86_64","provides":["ddddocr","cv2",...]}
  site-packages/     ddddocr/ cv2/ onnxruntime/ numpy/ ...
```

**构建**：`python packaging/fnos/build_captcha_pack.py 1.0`
（复用 `build/wheels/` 里已下好的 manylinux wheel，解压即用、不做二进制改动）
产物落在 `packaging/fnos/dist/captcha-local-pack-<ver>.zip`（约 130MB）。

### 16.4 程序内自动更新（updater.py）

**目标**：Web 上点一下就能升级，不用重新部署。

**协议**：更新源是 GitHub 仓库，根目录需有 `update_manifest.json`：

```json
{
  "name": "checkin-system",
  "version": "1.3.0",
  "released_at": "2026-09-20",
  "notes": "更新说明",
  "files": { "app/main.py": "sha256hex…", "har/render.py": "sha256hex…" }
}
```

程序比对 `version.json` 与清单里的 version，有新版则逐个下载 raw 文件、
校验哈希后写入。

**⚠️ 安全模型（必读，这不是玩具）**

这个模块能从网络下载代码并**覆盖自身正在运行的程序**，等价于远程代码执行能力。
四道闸：

| # | 闸 | 位置 | 作用 |
|---|---|---|---|
| 1 | 域名白名单 | `_assert_allowed_url` | 只允许 `github.com` / `raw.githubusercontent.com` / `codeload.github.com` / `api.github.com` 及其子域。**每次请求都校验**，改写源地址也绕不过 |
| 2 | SHA256 清单校验 | `run_update` 阶段 1 | 任一文件哈希不符 → **整批拒绝，不落地任何文件** |
| 3 | 路径白名单 | `validate_path` | 只允许 `app/ har/ plugins/ templates/ docs/` 与根目录白名单文件；**禁止** `data/`（数据库）、`.env`（密钥）、`user_plugins/`（用户插件）、`logs/`、`.git/`；扩展名白名单；拒绝 `..` 与绝对路径 |
| 4 | 备份 + 回滚 | `run_update` 阶段 2-3 | 写前备份到 `data/backups/<时间戳>/`，写入失败立即回滚 |

**已知局限（必须向用户说清，不要夸大安全性）**：
清单文件**没有签名**，只能防传输损坏，**防不住上游仓库被篡改**。
要防后者需要密钥签名，本项目未做。所以更新源必须是用户自己的仓库。

**两个必须知道的实现细节**：

1. `ROOT` 是**文件所在目录**（`dirname(abspath(__file__))`），不是父目录 ——
   `updater.py` 就放在项目根。早期写成 `dirname(dirname(__file__))` 会指到
   项目外一层，症状是 `build_manifest` 遍历到 0 个文件、`run_update` 把文件
   写到错误位置。**这个 bug 在测试里才暴露**（3.13 / 3.16）。
2. 更新后调 `load_all_plugins()` 重载插件，但 **Flask 的模块一旦 import 就不会
   重新执行** —— 所以涉及 `app/`、`har/` 的改动，页面会明确提示「建议重启容器」。
   不要谎称完全无需重启。

**发版流程**（改完代码后）：

```bash
# 方式一：Web 系统设置页 → 填版本号 → 点「下载清单」
# 方式二：命令行
python -c "import updater,json;print(json.dumps(updater.build_manifest('1.3.0','说明'),ensure_ascii=False,indent=2))" > update_manifest.json
```
然后把改动 + `update_manifest.json` 一起提交到仓库根目录。

### 16.4 `docker-compose.yml` 的代码挂载

```yaml
volumes:
  - ./:/app        # ← 必须有，否则容器重建后更新丢失
```

这是「更新后不用重新部署」的**前提**。依赖装在 site-packages 不在 `/app`，
所以挂载不会覆盖已安装依赖；但**改了 requirements.txt 仍需重建镜像**。

### 16.5 新增/改动的文件

| 文件 | 说明 |
|---|---|
| `captcha.py` | 新增。验证码识别统一入口 |
| `updater.py` | 新增。自动更新（含域名/路径白名单、哈希校验、备份回滚） |
| `version.json` | 新增。当前版本号 |
| `app/notifier.py` | 重写。所有渠道检查响应体并返回可读失败原因 |
| `app/routes/update_api.py` | 新增。更新相关 API + `/settings` 页面 |
| `templates/settings.html` | 新增。系统设置页 |
| `plugins/fuliba.py` | 改动。验证码识别接 captcha.py，新增 3 个表单字段 |
| `app/engine.py` | 改动。把通知失败原因写进任务日志 |
| `app/extras.py` | 新增（v1.4.0）。可选组件管理：安装/卸载/启停/注入 sys.path |
| `app/routes/extras_api.py` | 新增（v1.4.0）。`/api/extras/*` 接口 |
| `packaging/fnos/build_captcha_pack.py` | 新增（v1.4.0）。构建本地识别组件包 |
| `packaging/fnos/make_runtime_tgz.py` | 改动（v1.3.0）。ELF strip + 裁剪，771MB→113MB |

### 16.7 测试

```bash
python __har_test/selftest.py         # 57 项：har 渲染/执行
python __har_test/integration.py      # 41 项：har 端到端（mock HTTP）
python __har_test/test_features.py    # 85 项：PushPlus / 云码 / 更新安全闸
python __har_test/test_extras.py      # 38 项：可选组件安装/校验/穿越防护/启停/卸载
```

其中更新部分重点覆盖**攻击面**：目录穿越、绝对路径、`.env`/`data/`/`user_plugins/`
越权、非白名单扩展名、伪造域名（`raw.githubusercontent.com.evil.com`、
`github.com@evil.com`）、哈希不匹配时原文件必须零改动。

`test_extras.py` 重点覆盖**组件包的恶意/畸形输入**：非 zip、缺 manifest、
name 不符、zip slip 路径穿越、空 site-packages，以及"重复安装要清掉旧文件"
（否则新旧库混在一起会出诡异 bug）。

---

## 17. 相关文档

- `README.md` — 用户向快速部署。
- `docs/README.md` — 用户向完整手册（接口/数据库表/插件标准/任务配置/通知/FAQ）。
- 本文（`DEVELOPMENT.md`）— 开发向，面向续作者与 AI。
- `docs/hyperdown-handoff.md` — Hyperdown 插件的交接文档（可交给其他 AI 复刻）。

> 三份文档关系：README 给"怎么用"，docs/README 给"怎么配"，DEVELOPMENT 给"怎么改/怎么续作"。改代码后若涉及接口或表结构变化，同步更新对应文档。

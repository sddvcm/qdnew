# 签到管理系统 (Checkin System) 完整文档

## 目录

1. [概述](#1-概述)
2. [部署指南](#2-部署指南)
3. [数据库设计](#3-数据库设计)
4. [API 接口文档](#4-api-接口文档)
5. [插件开发标准](#5-插件开发标准)
6. [任务配置说明](#6-任务配置说明)
7. [通知系统](#7-通知系统)
8. [常见问题](#8-常见问题)

---

## 1. 概述

签到管理系统是一个轻量级、可扩展的自动签到平台，专为 Docker 环境（如飞牛 NAS）设计。

### 核心特性

| 特性 | 说明 |
|------|------|
| 插件化架构 | 每个签到目标是一个独立 Python 文件，丢进去就能用 |
| 动态表单 | 插件自声明需要哪些配置字段，前端自动渲染表单 |
| 敏感信息加密 | 密码和 Cookie 使用 AES-256-CBC 加密存储 |
| 定时调度 | APScheduler 驱动，每个任务独立 Cron 表达式 |
| 多渠道通知 | 支持 PushPlus / Server酱 / Bark / 钉钉 / 企业微信 / Telegram / Webhook |
| Web 管理界面 | 完整的任务列表、详情日历、日志查看 |
| 轻量 | Docker 镜像约 300MB+（含 ddddocr/onnxruntime），仅依赖 Python slim + Flask + SQLite |

### 技术栈

- **后端：** Python 3.12 + Flask
- **调度：** APScheduler (BackgroundScheduler)
- **数据库：** SQLite (WAL 模式)
- **加密：** cryptography (AES-256-CBC)
- **前端：** Jinja2 模板 + 原生 JavaScript + CSS3

---

## 2. 部署指南

### 2.1 环境要求

- Docker 20.10+ 和 Docker Compose v2
- 推荐飞牛 NAS、群晖、Unraid 等支持 Docker 的设备

### 2.2 部署步骤

#### 第一步：上传项目

将整个 `checkin-system` 目录上传到 NAS，例如：

```
/vol1/docker/checkin-system/
```

#### 第二步：修改密钥

编辑 `.env` 文件，将 `CHECKIN_SECRET_KEY` 改为随机字符串：

```bash
cd /vol1/docker/checkin-system
nano .env
```

```
CHECKIN_SECRET_KEY=你的一段随机字符串
```

#### 第三步：构建并启动

```bash
cd /vol1/docker/checkin-system
docker compose up -d
```

首次构建需要 1-3 分钟（下载 Python 基础镜像 + 安装依赖）。

#### 第四步：验证运行

```bash
# 查看状态
docker compose ps

# 查看日志
docker compose logs -f
```

看到 `Running on http://0.0.0.0:5800` 即启动成功。

#### 第五步：打开管理界面

浏览器访问：

```
http://你的NAS_IP:5800
```

### 2.3 常用运维命令

```bash
# 查看日志
docker compose logs -f

# 重启服务
docker compose restart

# 停止服务
docker compose down

# 更新代码后重建
docker compose up -d --build

# 进入容器调试
docker exec -it checkin-system /bin/bash
```

### 2.4 目录挂载说明

| 宿主机路径 | 容器路径 | 用途 |
|-----------|---------|------|
| `./data` | `/app/data` | SQLite 数据库持久化 |
| `./logs` | `/app/logs` | 日志文件 |
| `./user_plugins` | `/app/user_plugins` | 用户自定义插件 |

---

## 3. 数据库设计

### 3.1 表结构

#### plugins — 插件注册表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| name | TEXT UNIQUE | 插件唯一标识，如 "fuliba" |
| display_name | TEXT | 显示名称，如 "福利吧论坛签到" |
| description | TEXT | 插件描述 |
| version | TEXT | 版本号，默认 "1.0" |
| plugin_type | TEXT | 类型：http / selenium / playwright / api / custom |
| author | TEXT | 作者 |
| builtin | INTEGER | 是否内置插件（0=用户导入，1=系统内置） |
| enabled | INTEGER | 是否启用 |
| form_schema | TEXT | JSON 数组，定义前端表单字段 |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

**form_schema 格式示例：**

```json
[
  {
    "key": "site_url",
    "label": "论坛地址",
    "type": "url",
    "required": true,
    "default": "https://www.wnflb2023.com",
    "placeholder": "https://www.wnflb2023.com",
    "options": [],
    "help_text": "福利吧论坛当前可用域名",
    "sensitive": false
  },
  {
    "key": "cookie",
    "label": "Cookie",
    "type": "textarea",
    "required": true,
    "sensitive": true
  }
]
```

#### tasks — 签到任务表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| plugin_id | INTEGER FK | 关联 plugins.id |
| name | TEXT | 任务名称 |
| site_url | TEXT | 站点地址 |
| username | TEXT | 用户名 |
| password | TEXT | 密码（AES 加密） |
| cookie | TEXT | Cookie（AES 加密） |
| cron_expr | TEXT | Cron 表达式，默认 "0 9 * * *" |
| enabled | INTEGER | 是否启用 |
| status | TEXT | 状态：normal / error / expired / disabled |
| status_message | TEXT | 状态说明 |
| consecutive_fail | INTEGER | 连续失败次数 |
| max_fail_alert | INTEGER | 连续失败多少次后告警，默认 3 |
| last_result | TEXT | 最近结果：success / failed |
| last_message | TEXT | 最近签到消息 |
| last_extra | TEXT | 最近签到额外信息（JSON） |
| last_run_at | TEXT | 最近运行时间 |
| next_run_at | TEXT | 下次运行时间 |
| params | TEXT | 自定义参数（JSON，任意扩展） |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

**params 字段说明：**

这是整个系统的扩展核心。任何插件需要的自定义参数都存在这里，以 JSON 格式存储。例如：

```json
{
  "retry": 3,
  "formhash_regex": "formhash=([a-f0-9]+)",
  "proxy": "http://127.0.0.1:7890",
  "delay_min": 1,
  "delay_max": 5
}
```

**设计原则：永远不新增表字段，所有扩展参数放入 params。**

#### checkin_logs — 签到记录表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| task_id | INTEGER FK | 关联 tasks.id |
| checkin_date | TEXT | 签到日期，如 "2026-07-27" |
| checkin_time | TEXT | 签到时间，如 "09:00:03" |
| result | TEXT | 结果：success / failed |
| message | TEXT | 签到消息 |
| extra_info | TEXT | 额外信息（JSON） |
| duration_ms | INTEGER | 执行耗时（毫秒） |
| error_trace | TEXT | 失败时错误堆栈 |
| created_at | TEXT | 记录时间 |

**索引：** `idx_logs_task_date` (task_id, checkin_date)

**去重规则：** 同一任务同一天只有一条记录，重复执行会更新而非新增。

#### notify_configs — 通知配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| notify_type | TEXT | 类型：pushplus / serverchan / bark / webhook / dingtalk / wecom / telegram |
| name | TEXT | 自定义名称 |
| config | TEXT | 配置参数（JSON） |
| enabled | INTEGER | 是否启用 |

**config 字段示例：**

```json
// PushPlus
{"token": "your_pushplus_token"}

// Server酱
{"sendkey": "your_sendkey"}

// Bark
{"device_key": "your_key", "server_url": "https://api.day.app"}

// 钉钉
{"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxx", "secret": "SECxxx"}

// 企业微信
{"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"}

// Telegram
{"bot_token": "xxx:xxx", "chat_id": "123456"}

// 自定义Webhook
{"url": "https://example.com/hook", "headers": {"Authorization": "Bearer xxx"}, "body_template": "{{title}}\n{{body}}"}
```

#### task_notify — 任务通知关联表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| task_id | INTEGER FK | 关联 tasks.id |
| notify_id | INTEGER FK | 关联 notify_configs.id |
| on_success | INTEGER | 成功时是否通知（1=是） |
| on_failure | INTEGER | 失败时是否通知（1=是） |

#### system_config — 系统配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| key | TEXT PK | 配置键 |
| value | TEXT | 配置值 |
| updated_at | TEXT | 更新时间 |

### 3.2 ER 关系图

```
plugins (1) ----< (N) tasks
tasks (1) ----< (N) checkin_logs
tasks (N) >----< (N) notify_configs  (via task_notify)
```

---

## 4. API 接口文档

### 4.1 系统统计

#### GET /api/system/stats

获取系统运行统计。

**响应：**

```json
{
  "total_tasks": 3,
  "enabled_tasks": 2,
  "error_tasks": 1,
  "today_checkins": 2,
  "today_success": 2
}
```

| 字段 | 说明 |
|------|------|
| total_tasks | 任务总数 |
| enabled_tasks | 启用中的任务数 |
| error_tasks | 状态异常的任务数 |
| today_checkins | 今天有签到记录的任务数（去重） |
| today_success | 今天签到成功的任务数（去重） |

#### PUT /api/system/config

更新系统配置。

**请求：**

```json
{"key1": "value1", "key2": "value2"}
```

---

### 4.2 插件管理 — /api/plugins

#### GET /api/plugins

列出所有已注册插件。

**响应：**

```json
[
  {
    "id": 1,
    "name": "fuliba",
    "display_name": "福利吧论坛签到",
    "description": "Discuz! 论坛 fx_checkin 插件自动签到",
    "plugin_type": "http",
    "form_schema": "[...]",
    "builtin": 1,
    "enabled": 1
  }
]
```

#### GET /api/plugins/:id/schema

获取插件的表单 schema（用于前端动态渲染表单）。

**响应：**

```json
[
  {"key": "site_url", "label": "论坛地址", "type": "url", "required": true},
  {"key": "cookie", "label": "Cookie", "type": "textarea", "required": true, "sensitive": true},
  {"key": "retry", "label": "重试次数", "type": "number", "default": 3}
]
```

#### POST /api/plugins/import

导入用户自定义插件。

**请求：**

```json
{
  "code": "from plugins.base import BasePlugin, ...\nclass MyPlugin(BasePlugin):\n    ...",
  "filename": "my_checkin.py"
}
```

**响应：**

```json
{
  "success": true,
  "message": "插件 我的签到插件 加载成功",
  "plugin": {
    "name": "my_checkin",
    "display_name": "我的签到插件",
    "plugin_type": "http",
    "filename": "my_checkin.py"
  }
}
```

#### DELETE /api/plugins/:name

删除用户插件（仅限非内置插件）。

---

### 4.3 任务管理 — /api/tasks

#### GET /api/tasks

列出所有任务。

**响应：** 任务对象数组，字段见数据库 tasks 表。

#### GET /api/tasks/:id

获取单个任务详情。

#### POST /api/tasks

创建签到任务。

**请求：**

```json
{
  "plugin_id": 1,
  "name": "福利吧每日签到",
  "site_url": "https://www.wnflb2023.com",
  "username": "myuser",
  "password": "",
  "cookie": "完整Cookie字符串",
  "cron_expr": "0 9 * * *",
  "enabled": 1,
  "params": {
    "retry": 3
  }
}
```

**响应：** 201 Created，返回完整任务对象。

#### PUT /api/tasks/:id

更新任务。只需传要修改的字段。

#### DELETE /api/tasks/:id

删除任务（同时从调度器移除）。

#### POST /api/tasks/:id/run

手动触发立即签到。

**响应：**

```json
{
  "success": true,
  "message": "签到成功，您是今日第3874个签到",
  "extra": {"days": 2851, "consecutive_days": 30}
}
```

#### GET /api/tasks/:id/logs?limit=365

获取签到日志列表。

#### GET /api/tasks/:id/calendar?year=2026&month=7

获取签到日历数据。

**响应：**

```json
{
  "2026-07-01": "success",
  "2026-07-02": "success",
  "2026-07-03": "failed",
  "2026-07-04": "success"
}
```

---

### 4.4 通知配置 — /api/notify

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/notify | 列出所有通知配置 |
| POST | /api/notify | 创建通知配置 |
| PUT | /api/notify/:id | 更新通知配置 |
| DELETE | /api/notify/:id | 删除通知配置 |
| GET | /api/notify/task/:task_id | 获取任务关联的通知 |
| PUT | /api/notify/task/:task_id | 设置任务通知关联 |

---

## 5. 插件开发标准

### 5.1 插件文件结构

每个插件是一个独立的 `.py` 文件，必须包含一个继承 `BasePlugin` 的类。

```
plugins/
├── base.py          # 基类（不要修改）
├── fuliba.py        # 福利吧签到
├── huangguaba.py    # 黄瓜吧签到
└── your_plugin.py   # 你的新插件
```

### 5.2 BasePlugin 基类接口

```python
from plugins.base import BasePlugin, FormField, CheckinResult
```

#### FormField 数据类

| 属性 | 类型 | 必填 | 说明 |
|------|------|------|------|
| key | str | 是 | 字段标识，如 "cookie" |
| label | str | 是 | 显示标签，如 "Cookie" |
| type | str | 否 | 字段类型，默认 "text"。支持：text / password / url / textarea / select / checkbox / number |
| required | bool | 否 | 是否必填，默认 False |
| default | Any | 否 | 默认值 |
| placeholder | str | 否 | 输入框占位文字 |
| options | List[Dict] | 否 | select 类型的选项，如 [{"value":"a","label":"选项A"}] |
| help_text | str | 否 | 帮助说明文字 |
| sensitive | bool | 否 | 是否敏感字段（加密存储、前端小眼睛），默认 False |

#### CheckinResult 数据类

| 属性 | 类型 | 说明 |
|------|------|------|
| success | bool | 签到是否成功 |
| message | str | 人类可读的签到结果消息 |
| extra | Dict | 额外信息，如积分、天数等 |

#### BasePlugin 抽象类

**必须覆盖的类属性：**

```python
name: str = ""           # 唯一标识，如 "fuliba"
display_name: str = ""   # 显示名称，如 "福利吧论坛签到"
description: str = ""    # 描述
plugin_type: str = "http" # 类型：http / selenium / playwright / api / custom
form_schema: List[FormField] = []  # 表单字段声明
```

**必须实现的方法：**

```python
def checkin(self, task_config: dict) -> CheckinResult:
    """执行签到"""
    pass
```

**task_config 参数结构：**

```python
{
    "username": "用户填写的用户名",
    "password": "用户填写的密码（已解密）",
    "cookie": "用户填写的 Cookie（已解密）",
    "site_url": "用户填写的站点地址",
    "params": {
        # 用户填写的所有插件专属参数
        "retry": 3,
        ...
    }
}
```

**可选覆盖的方法：**

```python
def pre_checkin(self, task_config: dict) -> Optional[CheckinResult]:
    """签到前检查。返回 CheckinResult 表示提前终止，返回 None 表示继续"""
    return None

def post_checkin(self, task_config: dict, result: CheckinResult) -> CheckinResult:
    """签到后处理，可修改结果"""
    return result
```

### 5.3 完整插件示例

```python
"""我的论坛签到插件"""
import re
import requests
from plugins.base import BasePlugin, FormField, CheckinResult


class MyForumPlugin(BasePlugin):
    name = "my_forum"
    display_name = "我的论坛签到"
    description = "Discuz! 论坛签到"
    plugin_type = "http"
    author = "your_name"

    form_schema = [
        FormField(key="site_url", label="论坛地址", type="url", required=True,
                  placeholder="https://example.com"),
        FormField(key="cookie", label="Cookie", type="textarea", required=True,
                  placeholder="从浏览器复制完整Cookie", sensitive=True),
        FormField(key="retry", label="重试次数", type="number", required=False,
                  default=3),
    ]

    def checkin(self, config):
        params = config.get("params", {})
        site_url = config.get("site_url", "") or params.get("site_url", "")
        cookie = config.get("cookie", "") or params.get("cookie", "")
        retry = int(params.get("retry", 3))

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 ...",
            "Cookie": cookie,
        })

        for attempt in range(retry):
            try:
                # 1. 获取 formhash
                resp = session.get(f"{site_url}/plugin.php?id=fx_checkin:list", timeout=15)
                match = re.search(r'formhash=([a-f0-9]+)', resp.text)
                if not match:
                    if attempt == retry - 1:
                        return CheckinResult(False, "无法获取formhash")
                    continue

                formhash = match.group(1)

                # 2. 执行签到
                resp = session.get(
                    f"{site_url}/plugin.php?id=fx_checkin:checkin",
                    params={"formhash": formhash, "inajax": 1},
                    timeout=15,
                )

                # 3. 判断结果
                if "签到成功" in resp.text:
                    return CheckinResult(True, "签到成功", {"raw": resp.text[:200]})

                if "已经签到" in resp.text:
                    return CheckinResult(True, "今日已签到", {"already": True})

                return CheckinResult(False, f"签到失败: {resp.text[:200]}")

            except requests.RequestException as e:
                if attempt == retry - 1:
                    return CheckinResult(False, f"网络错误: {e}")
                continue

        return CheckinResult(False, "重试次数已用完")
```

### 5.4 支持的签到类型

| plugin_type | 说明 | 适用场景 | 额外依赖 |
|-------------|------|---------|---------|
| http | HTTP 请求模拟 | 论坛签到、API 签到 | requests |
| selenium | 浏览器自动化 | 复杂 JS 渲染、验证码 | selenium + webdriver |
| playwright | 无头浏览器 | 现代 SPA 页面 | playwright |
| api | 纯 API 调用 | 开放平台签到 | 按需 |
| custom | 自定义类型 | 特殊场景 | 按需 |

### 5.5 导入插件的方式

1. **Web 界面导入：** 点击「导入插件」→ 粘贴代码或上传 `.py` 文件
2. **手动放入目录：** 将 `.py` 文件放入 `user_plugins/` 目录，重启容器即可

### 5.6 单插件深度开发文档（交接级）

对于协议复杂或需要逆向的站点，本目录另附**可交给其他 AI Agent 完整复刻**的深度开发文档：

| 文档 | 对应插件 | 说明 |
|------|---------|------|
| [hyperdown-handoff.md](./hyperdown-handoff.md) | `plugins/hyperdown.py` | Hyperdown 网盘签到：账号密码登录 + SealJSON 加密信封（X25519 ECDH + HKDF + XChaCha20-Poly1305 + HMAC）。含完整接口契约、逐字节算法、踩坑史、重写检查清单 |

> ⚠️ **注意**：Hyperdown 插件依赖 `pynacl`（已在 `requirements.txt`）。若你的镜像构建早于该依赖加入，**必须重建镜像**，否则插件 import 阶段会失败。

---

## 6. 任务配置说明

### 6.1 任务表单字段

#### 通用字段（所有插件共享）

| 字段 | 必填 | 说明 |
|------|------|------|
| 插件选择 | 是 | 下拉选择已注册的签到插件 |
| 任务名称 | 是 | 自定义名称，如 "福利吧每日签到" |
| 站点地址 | 否 | 目标网站 URL |
| 用户名 | 否 | 登录用户名（仅作备注） |
| 密码 | 否 | 登录密码（AES 加密存储） |
| Cookie | 否（选填） | 登录后的 Cookie（AES 加密存储）；不填可用「用户名+密码」自动登录，登录成功后会自动保存并复用 Cookie |
| Cron 表达式 | 是 | 定时规则，默认每天 09:00 |
| 启用 | 是 | 是否启用此任务 |

#### 插件专属参数

根据所选插件的 `form_schema` 动态渲染。通用字段中已有的（cookie、username、password、site_url）不会在专属参数区域重复出现。

### 6.2 Cron 表达式格式

```
分 时 日 月 星期
```

| 表达式 | 含义 |
|--------|------|
| `0 9 * * *` | 每天 09:00 |
| `0 8 * * *` | 每天 08:00 |
| `0 12 * * *` | 每天 12:00 |
| `0 9 * * 1-5` | 工作日 09:00 |
| `0 */6 * * *` | 每 6 小时 |
| `30 7 * * *` | 每天 07:30 |
| `0 9 1 * *` | 每月 1 号 09:00 |

### 6.3 如何获取 Cookie

1. 浏览器打开目标网站并登录
2. 按 **F12** 打开开发者工具
3. 点击 **Application**（应用程序）标签
4. 左侧选择 **Cookies** → 选择网站域名
5. 将所有的 Name=Value 对用 `; ` 连接复制
6. 粘贴到任务表单的 Cookie 字段

### 6.4 任务状态说明

| 状态 | 显示 | 触发条件 |
|------|------|---------|
| normal | 绿色「正常」 | 最近一次签到成功 |
| error | 红色「异常」 | 最近一次签到失败 |
| expired | 黄色「过期」 | Cookie 过期 |
| disabled | 灰色「未启用」 | enabled=0 |

---

## 7. 通知系统

### 7.1 支持的通知渠道

| 渠道 | notify_type | 配置参数 |
|------|------------|---------|
| PushPlus | pushplus | token |
| Server酱 | serverchan | sendkey |
| Bark (iOS) | bark | device_key, server_url(可选) |
| 钉钉机器人 | dingtalk | webhook_url, secret(可选) |
| 企业微信机器人 | wecom | webhook_url |
| Telegram Bot | telegram | bot_token, chat_id |
| 自定义 Webhook | webhook | url, headers(可选), body_template(可选) |

### 7.2 通知策略

- 每个任务可关联多个通知渠道
- 可分别控制成功/失败时是否通知
- 通知内容包含：任务名称、签到结果、签到消息、额外信息

---

## 7.5 验证码识别（云码 / 本地）

登录类网站（如福利吧）触发验证码时需要识别。有两种方式：

| 方式 | 说明 | 前提 |
|---|---|---|
| **云码 jfbym** | 云端识别，准确率高，按次计费（约 3 积分/次） | 去 [www.jfbym.com](https://www.jfbym.com) 注册拿 Token |
| **本地识别** | 本机跑 ddddocr + ONNX，免费、不联网、无额度限制 | 需先安装「本地识别组件包」（见下） |

### 识别方式怎么选

进 **系统设置 → 本地验证码识别**，选「识别方式（全局默认）」：

- **云码**：开箱可用，只需填 Token
- **仅本地识别**：必须先安装组件包，否则提示无法选择
- **本地优先，失败转云码**：先本地（省钱），本地不可用或失败时自动改用云码

> 任务表单里的「验证码识别方式」**留空**即跟随这里的全局设置；
> 某个任务要特殊处理时，在任务里单独选即可（以任务为准）。
>
> 云码 Token 填在系统设置里可让所有任务共用，不必每个任务填一遍。

### 安装本地识别组件（可选，约 180MB）

因为本地识别依赖很大（ddddocr + OpenCV + ONNX + NumPy，解压约 400MB），
为了让主安装包保持精简（37MB），这部分做成了**按需上传的组件包**。

1. 拿到组件包 `captcha-local-pack-*.zip`（约 180MB）
2. 进 **系统设置 → 本地验证码识别 → 上传本地识别组件包**，选择该 zip
3. 点「上传并安装」，等进度条走完（自动启用，**无需重启**）
4. 点「测试本地识别」确认可用，再把识别方式切到「仅本地」或「本地优先」

安装后组件存在**数据目录**里，**升级/重装程序不会丢**，不用每次重传。
不想要了点「卸载组件」即可（会删除已上传的文件）。

### 常见问题

**Q: 没有组件包去哪拿？**
组件包由本项目发版时生成（`packaging/fnos/build_captcha_pack.py`）。
如果你是从源码自行部署，跑一下这个脚本就能得到。

**Q: 可以用云码但填了本地识别？**
本地组件没装时，「仅本地」选项是灰的；若任务里单独选了 local 而组件没装，
任务会失败并提示「未安装本地识别组件」——按提示装组件或改用云码即可。

**Q: 云码调用频率高吗？**
不高。**Cookie 直连模式完全不碰验证码**，只有账号密码登录且被站点风控要求
验证码时才调用。日常签到基本是 0 消耗。

---

## 8. 常见问题

### Q: Cookie 多久过期？

Discuz 论坛 Cookie 通常有效期 1-4 周。过期后签到会提示"Cookie已过期"，在任务列表点「编辑」更新 Cookie 即可。

### Q: 可以同时签多个论坛吗？

可以。每个论坛创建一个任务，选择对应的插件（或导入新插件），填各自的 Cookie。

### Q: 怎么把配置搬到另一台机器 / 备份任务？

用**「系统设置 → 任务导出 / 导入」**：

1. **导出**：勾选导出范围（任务 / HAR 模板 / 通知渠道 / 系统设置），点「导出配置文件」，
   得到一个 `.qdpack` 文件
2. **导入**：在新机器上选择该文件 → 点「读取并预览」确认内容 → 选同名处理方式 → 确认导入

导出文件是**加密的**，里面含账号密码，但**只有本程序能打开**——
用文本编辑器、解压软件都读不出内容（实测非 JSON、非压缩包、无明文密码）。

**换机导入的前提**：两边的 `CHECKIN_SECRET_KEY` 必须一致（同一个 fpk 升级/重装不会变）。
如果提示"无法解密"，说明密钥不同——把旧机的
`etc/app.env` 里的 `CHECKIN_SECRET_KEY` 复制到新机即可。

> ⚠️ 导出文件含明文密码（在加密层内），请当敏感文件对待，别放公共网盘。

### Q: 数据存在哪里？（飞牛 fpk 安装）

放在**共享目录**里，可以直接在 **文件管理** 中找到：

```
<安装卷>/@appshare/checkin-system/
├── data/            ← 数据库 checkin.db、更新前自动备份
├── user_plugins/    ← 你自己上传的插件
└── ...              ← 导出的 .qdpack 等
```

打开「**系统设置 → 环境信息**」可以看到实际路径与状态：

| 显示 | 含义 |
|---|---|
| 数据目录 | 实际路径，可复制到文件管理里打开 |
| 落点来源 | 共享目录（文件管理可见）/ 私有目录（看不到） |
| 可写 | 必须是 ✔；若是 ✘ 说明目录权限有问题 |
| 共享目录 | 列出各候选路径的探测结果，便于定位问题 |

> 如果显示"私有目录"，说明共享目录没接上（应用仍可正常使用，只是数据
> 在系统私有目录里，文件管理看不到）。把「环境信息」的内容反馈给开发者即可。

也可以从**路径**直接找：

```bash
# 兼容入口（官方约定）
/var/apps/checkin-system/share/checkin-system/
```

### Q: 如何备份数据？

**飞牛 fpk 安装**：数据在共享目录，用文件管理直接复制即可：

```
文件管理 → 存储空间 → @appshare → checkin-system
```

**Docker 部署**：数据库和用户插件在挂载的卷中：

```bash
# 备份
cp -r /vol1/docker/checkin-system/data /backup/checkin-data-$(date +%Y%m%d)

# 恢复
cp -r /backup/checkin-data-xxx/* /vol1/docker/checkin-system/data/
```

### Q: 如何查看详细日志？

```bash
# 实时日志
docker compose logs -f

# 最近 100 行
docker compose logs --tail=100
```

### Q: 如何添加新论坛的签到？

1. 写一个继承 `BasePlugin` 的插件文件（参考第 5 章）
2. 通过 Web 界面「导入插件」上传
3. 在「添加任务」中选择该插件，填写 Cookie 等配置

### Q: 端口被占用怎么办？

修改 `docker-compose.yml` 中 `ports` 的左边端口号：

```yaml
ports:
  - "5801:5800"   # 改为其他端口
```

### Q: 签到记录能保存多久？

永久保存，SQLite 数据库不做自动清理。如需清理，可删除旧记录或手动 SQL：

```sql
DELETE FROM checkin_logs WHERE checkin_date < date('now', '-365 days');
```

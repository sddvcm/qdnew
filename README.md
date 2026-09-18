# checkin-system 签到管理系统

轻量级、插件化的自动签到平台。Docker 部署，Web 管理，支持三种扩展方式：
内置插件、HAR 抓包模板（不写代码）、用户上传插件。

## 特性

- **HAR 通用签到**：浏览器抓包 → 导入 → 界面配置变量与断言 → 定时跑，**无需写代码**
- **专用插件**：处理需要加密签名 / 验证码 / 浏览器渲染的站点
- **验证码识别**：本地 ddddocr 或云码 jfbym，支持「本地优先失败转云码」
- **7 种通知渠道**：PushPlus / Server酱 / Bark / 钉钉 / 企业微信 / Telegram / Webhook
- **程序内自动更新**：Web 里检查更新并升级，不用重新部署
- **凭据加密**：账号密码与 Cookie 用 AES-256-CBC 加密存储
- **插件热导入**：Web 上传 `.py` 即时生效

## 快速开始

```bash
# 1. 配置密钥
cp .env.example .env   # 或直接编辑 .env
# 把 CHECKIN_SECRET_KEY 改成一串随机字符（生产必须改）

# 2. 启动
docker compose up -d

# 3. 访问
# http://你的NAS_IP:5800
```

## 三种加站点的方式

| 场景 | 方式 |
|---|---|
| 普通 HTTP 请求签到 | **HAR 模板**（推荐）—— 抓包导入即可 |
| 有动态加密签名 | 写专用插件（参考 `plugins/hyperdown.py`） |
| 需要验证码 OCR | 写专用插件（参考 `plugins/fuliba.py`） |

判断标准：抓包看请求里有没有「每次都不一样、由页面 JS 算出来」的签名值。
**有 → 写插件；没有 → HAR 模板足够。**

## 文档

- **`README.md`** — 部署与使用（含 HAR 模板、通知、验证码、自动更新的配置步骤）
- **`docs/README.md`** — 完整用户手册（接口 / 数据库 / 插件标准 / FAQ）
- **`DEVELOPMENT.md`** — 开发文档，面向续作者与 AI（含历史踩坑与设计约束）

## 目录结构

```
checkin-system/
├── app/              # Flask 后端（路由 / 模型 / 引擎 / 调度 / 通知）
├── har/              # HAR 通用签到引擎（解析 / 渲染 / 执行）
├── plugins/          # 内置插件（base / fuliba / hyperdown / har_template）
├── user_plugins/     # 用户导入的插件
├── templates/        # Jinja2 页面
├── captcha.py        # 验证码识别统一入口
├── updater.py        # 程序内自动更新
├── version.json      # 当前版本
├── update_manifest.json  # 更新清单（发版时同步）
└── __har_test/       # 测试（178 项，纯本地 mock）
```

## 测试

```bash
python __har_test/selftest.py        # 57 项：渲染 / HAR 解析 / 执行引擎
python __har_test/integration.py     # 41 项：Flask 端到端
python __har_test/test_features.py   # 80 项：通知 / 验证码 / 更新安全闸
```

## 许可

私人项目，未附许可证。

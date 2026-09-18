# 飞牛 fnOS FPK 打包说明

> 本目录把签到管理系统打成飞牛应用中心的 `.fpk` 原生安装包：
> **内置完整 Python 运行时与全部依赖，安装零联网**（不走 Docker）。

## 包结构

```
packaging/fnos/
├── manifest              # fpk 元数据（版本/checksum 由 build_fpk.py 自动填充）
├── ICON.PNG / ICON_256.PNG   # 包图标（make_icons.py 生成）
├── config/
│   ├── privilege         # 以专用包用户运行（run-as=package，最小权限）
│   └── resource          # {}（无共享目录/docker 声明）
├── cmd/                  # 生命周期脚本（⚠️ 全部需要 LF + 执行位）
│   ├── common.sh         # 共享函数（解压运行时/目录初始化/密钥生成）
│   ├── main              # start/stop/status（PID 管理）
│   ├── install_init      # 安装前：x86_64 架构与磁盘空间预检
│   ├── install_callback  # 安装后：解压运行时→建数据目录→生成密钥
│   ├── upgrade_init      # 升级前：迁移旧数据
│   ├── upgrade_callback  # 升级后：重解压运行时（数据不动）
│   ├── uninstall_*       # 卸载（向导可选是否删数据）
│   └── config_*          # 配置变更（占位）
├── wizard/install|uninstall  # 安装/卸载向导
├── payload/ui/           # 桌面入口（config + 图标），打进 app.tgz
├── prepare_runtime.py    # 步骤1：下载运行时 tar + 依赖 wheel
├── make_runtime_tgz.py   # 步骤2：流式改造出 runtime.tar（保留软链/+x）
├── build_fpk.py          # 步骤3：组装 fpk + 结构自检
└── build/                # 中间产物（已 gitignore）
    ├── python-runtime.tar.gz
    ├── wheels/
    ├── site-packages-staging/
    ├── runtime.tar
    └── app.tgz
```

## 打包步骤（Windows 开发机上）

```bash
# 0. 依赖：Python 3.12+ 与 Pillow（生成图标用）
pip install Pillow

# 1. 下载运行时（多源自适应，~106MB）
python packaging/fnos/download_runtime.py

# 2. 下载全部依赖的 Linux x86_64 wheel
#    ⚠️ 必须 --only-binary 且多平台标签（onnxruntime 需要 manylinux_2_28）
python -m pip download \
  --dest packaging/fnos/build/wheels \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
  --platform manylinux_2_27_x86_64 --platform manylinux_2_28_x86_64 \
  --python-version 3.12 --implementation cp \
  --only-binary=:all: --no-cache-dir \
  -r requirements.txt

# 3. 生成图标（如未生成）
python packaging/fnos/make_icons.py

# 4. 构建运行时 tar（流式改造：保留软链与执行位）
python packaging/fnos/make_runtime_tgz.py

# 5. 组装 fpk（含结构自检）
python packaging/fnos/build_fpk.py
# 输出 packaging/fnos/dist/checkin-system-<version>.fpk
```

## 为什么这样设计（关键决策，勿随意改动）

### 1. 运行时在 NAS 上解压，而不是打包机上展开
Windows 文件系统无法表达 Linux 的执行位与软链。`python3 → python3.12` 软链和
`bin/python3.12 +x` 若在 Windows 生成再压缩，到 NAS 上就是坏的。
所以 payload 里放的是**原始 tar 流**，由 `install_callback` 在 Linux 上
`tar -xf` 解压 —— 权限、软链一步到位。

### 2. make_runtime_tgz.py 的"流式改造"
直接重新 tar 一个 Windows 上解出来的目录树会丢失全部元数据。正确做法是
逐成员读原始 tar：软链只复制元数据（linkname 在 tar 头里，Windows 也能读到），
普通文件从原始流拷贝（保留 mode），再追加 wheel 解包出的 site-packages。

### 3. 数据落在 var（@appdata），密钥落在 etc（@appconf）
飞牛 fpk 升级只覆盖 target（@appcenter）。把数据库、用户插件、备份放
`$TRIM_PKGVAR`，密钥放 `$TRIM_PKGETC`，升级/重装都不丢。
项目代码通过环境变量 `CHECKIN_DATA_DIR` / `CHECKIN_USER_PLUGINS_DIR`
重定向这两个落点（默认值保持 Docker/本地行为不变）。

### 4. 密钥自动生成且永不变
`install_callback` 首次安装用 `/dev/urandom` 生成 64 位十六进制密钥写入
`etc/app.env`。**已存在则绝不覆盖** —— 密钥一变，已存的账号密码全解不开。

### 5. run-as=package + chown
应用以专用包用户运行（官方推荐最小权限）。`install_callback` 若以 root 执行，
会把 app 目录 chown 给包用户 —— 否则「程序内自动更新」（包用户身份）写不了代码文件。

### 6. 程序内更新与 fpk 升级的关系
- **程序内更新**（Web 系统设置）：只更新代码文件（app/har/plugins/templates），
  适用于小修小补，更新后需在应用中心重启应用。
- **fpk 升级**（应用中心）：整包替换 target + 重解压运行时，适用于大版本/依赖变化。
- 两者可共存；fpk 升级会覆盖程序内更新过的代码（以 fpk 为准）。

## 已知限制

- **仅 x86_64**：manifest `platform=x86`；ARM 机型装不了（install_init 会拦截并提示）。
- **固定端口 5800**：被占用时安装报错（checkport）。改端口需编辑 `etc/app.env`
  的 `APP_PORT` 后在应用中心重启应用。
- **依赖变更必须重打 fpk**：程序内更新只改代码；新增 pip 依赖需要重新走一遍打包。
- **未在真机验证**：打包流程全部本地自检（build_fpk.py 会回读校验结构与权限位），
  但首次安装请在测试机上确认。

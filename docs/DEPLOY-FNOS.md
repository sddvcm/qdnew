# 飞牛 NAS (fnOS) Docker Compose 部署

> 本文假设你已在飞牛 NAS 上装好 Docker（应用中心 → Docker 或 `fnOS` 自带的 Docker 套件）。

---

## 一、准备：把文件传到 NAS

`checkin-system` 整个目录传到 NAS，例如放到：

```
/vol1/docker/checkin-system
```

**传哪些文件**：整个目录都传（含 `app/`、`har/`、`plugins/`、`templates/`、`Dockerfile`、
`docker-compose.yml`、`requirements.txt`）。

**不用传的**：`data/`（本地数据库，NAS 上会自动生成）、`.git/`、`__har_test/`、`__pycache__/`。

> 关于 `data/`：如果你在本机已经配好了任务，想连数据一起搬过去，那 `data/checkin.db`
> 要传，并且 **NAS 上的 `.env` 密钥必须与本机完全一致**，否则数据库里的密码解不出来。

---

## 二、改密钥（必做）

在 NAS 上进入目录，从模板生成 `.env`：

```bash
cd /vol1/docker/checkin-system
cp .env.example .env
```

然后编辑 `.env`，把 `CHECKIN_SECRET_KEY` 改成一串随机值：

```bash
# 推荐：直接命令生成
echo "CHECKIN_SECRET_KEY=$(openssl rand -hex 32)" > .env
cat .env
```

**为什么必须改**：这个密钥用于加密任务里的账号密码和 Cookie。用默认值等于没加密。

**⚠️ 三条铁律**：
1. 一旦开始使用，**不要更改**——改了已存的密码就解不出来，需要重新填一遍
2. 不要泄露：拿到密钥 + `data/checkin.db` 就能解出你所有账号
3. 备份 `data/` 时，`.env` 要一起备份，否则恢复后密码全废

---

## 三、启动

```bash
cd /vol1/docker/checkin-system
docker compose up -d --build
```

- `--build`：第一次必须加（要构建镜像）
- 首次构建约 2~5 分钟（要装 `ddddocr`/`onnxruntime`，体积较大）

**看日志确认起来了**：

```bash
docker compose logs -f
```

看到类似这样的输出就成功了：

```
* Running on all addresses (0.0.0.0)
* Running on http://0.0.0.0:5800
```

`Ctrl+C` 退出日志（不会停容器）。

---

## 四、访问

浏览器打开：

```
http://NAS的IP:5800
```

例如 `http://192.168.1.100:5800`。

---

## 五、常见命令

```bash
# 查看状态
docker compose ps

# 看日志（实时）
docker compose logs -f

# 只看最近 100 行
docker compose logs --tail=100

# 停止
docker compose down

# 停止并删除数据卷（⚠️ 会删数据库，慎用）
docker compose down -v

# 重启
docker compose restart

# 改了 requirements.txt 后重建（装了新依赖）
docker compose up -d --build

# 只改代码没改依赖 → 不用重建，重启即可
docker compose restart
```

---

## 六、升级

**方式一：Web 内一键更新（推荐）**

1. 系统设置 → 填更新源 `https://github.com/sddvcm/qdnew` → 保存
2. 点「检查更新」→ 有新版本点「立即更新」
3. 若改的是核心模块（`app/`、`har/`），执行 `docker compose restart` 完全生效

> 前提：`docker-compose.yml` 里有 `- ./:/app` 挂载（本项目已默认加好）。
> 没有这行的话，更新写进去的文件在容器重建后会丢失。

**方式二：手动拉代码**

```bash
cd /vol1/docker/checkin-system
git pull          # 如果 NAS 上是 git clone 的
docker compose restart
```

---

## 七、端口冲突怎么办

如果 5800 被占用，改 `docker-compose.yml`：

```yaml
ports:
  - "5801:5800"     # 左边是 NAS 端口，右边是容器内端口（别改）
```

然后 `docker compose up -d`。访问 `http://NAS的IP:5801`。

---

## 八、排查问题

### 构建失败

**症状**：`pip install` 报错、或 `onnxruntime` 装不上

**原因与处理**：
- **ARM 架构 NAS**（部分飞牛机型）：`onnxruntime` 需要 aarch64 wheel。检查架构：
  ```bash
  uname -m          # x86_64 或 aarch64
  ```
  若是 aarch64，容器构建通常也能成功（onnxruntime 有 aarch64 wheel），但会慢。
- **网络问题**：Dockerfile 里已配置清华 pip 源。若仍超时，检查 NAS 的 DNS 与外网连通性。

### 容器起来了但页面打不开

```bash
# 1. 容器是否在运行
docker compose ps

# 2. 容器内部是否能访问自己
docker compose exec checkin python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:5800/').status)"

# 3. 看有没有报错
docker compose logs --tail=50
```

- 容器 `Exited` → 看日志找原因（常见：密钥没配、端口被占、`data/` 权限问题）
- 容器 `Up` 但外部打不开 → 飞牛防火墙 / 端口未放行，或端口映射写错

### 权限问题

如果 `data/` 目录无法写入（日志报 `unable to open database file`）：

```bash
cd /vol1/docker/checkin-system
sudo chown -R 1000:1000 data logs user_plugins
# 或者放开权限（内网环境可接受）
chmod -R 777 data logs user_plugins
```

### 想看容器里到底发生了什么

```bash
# 进容器
docker compose exec checkin bash

# 看数据库
docker compose exec checkin ls -la /app/data
```

---

## 九、备份建议

需要备份的只有两样：

| 路径 | 内容 |
|---|---|
| `data/` | 数据库（任务配置、加密的账号密码、签到记录） |
| `.env` | 加密密钥（**必须与数据库一起备份**） |

```bash
# 简单备份
cd /vol1/docker
tar czf checkin-backup-$(date +%Y%m%d).tar.gz \
    checkin-system/data checkin-system/.env
```

> ⚠️ 只备份 `data/` 而不备份 `.env`，恢复后所有密码都解不出来。

---

## 十、完整命令速查

```bash
# ===== 首次部署 =====
cd /vol1/docker/checkin-system
cp .env.example .env
echo "CHECKIN_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build
docker compose logs -f

# ===== 日常 =====
docker compose ps                 # 状态
docker compose logs --tail=100    # 日志
docker compose restart            # 重启
docker compose down               # 停止

# ===== 升级 =====
# Web 里点「立即更新」，然后：
docker compose restart
```

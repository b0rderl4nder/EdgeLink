# 新设备接入与0号机迁移

## 新设备标准流程

### 1. 基础检查

确认：

- systemd与NetworkManager运行。
- 无线接口名称及AP能力。
- Python版本不低于3.10。
- EasyTier已按正式配置安装为systemd实例。
- 设备时间正确。

运行：

```bash
sudo edgelinkctl preflight
```

若新设备还没有EasyTier，先准备与CPU架构匹配的官方release归档及已审核配置，再执行：

```bash
sudo ./scripts/enroll_easytier.sh <归档> <配置> default
```

不允许安装脚本自动加入未知公共网络，也不允许把网络名或密钥写入仓库和诊断输出。

所有`error`项必须通过；`warning`需要人工确认后记录。

### 2. 配置

编辑`/etc/edgelink/config.json`：

- `device_name`：稳定且唯一。
- `wifi.interface`：实际无线接口。
- `health.targets`：至少一个设备确实需要访问的稳定节点。
- `overlay.service_unit`：实际EasyTier unit。
- `overlay.expected_cidr_prefix`：本项目虚拟网段。
- `api.trusted_mutation_subnets`：只放本机与配网网段。

AP密码位于`/etc/edgelink/secrets.env`，不要放入配置仓库。首次安装必须由设备交付清单显式提供，并把热点名和密码交付给管理员；禁止生成只留在设备内、管理员无法获知的随机密码。

### 3. 验收矩阵

| 场景 | 预期 |
|---|---|
| 正常Wi‑Fi启动 | 进入online，EasyTier在线 |
| 密码错误的新Wi‑Fi | 切换失败后AP恢复 |
| 同SSID的手机热点 | 重新走DHCP，不继承旧静态路由 |
| Wi‑Fi已关联但无网关 | 进入recovering，最终发出AP |
| DNS失败 | 不误报online |
| EasyTier进程退出 | Wi‑Fi保持，EasyTier自动启动 |
| EasyTier进程活着但无Peer | 冷却后重启EasyTier，不启动AP |
| 网络抖动 | 不在Wi‑Fi和AP间来回切换 |
| 断电重启 | 无需SSH即可恢复或出现AP |

## 0号机特殊迁移步骤

0号机已有Reachy daemon热点接口，不能直接让两个组件同时自动控制AP。

1. 只读记录实际NetworkManager连接、路由、DNS、无线接口和AP能力。
2. 找到EasyTier真实进程、systemd unit、配置路径与虚拟地址；禁止在终端输出密钥。
3. 查明Reachy daemon运行期是否自动触发`setup_hotspot`，以及触发窗口。
4. 第一阶段只运行preflight和单次状态检查，不启用EdgeLink自动服务。
5. 选择唯一AP控制者：推荐EdgeLink负责自动回退，Reachy daemon保留显式扫描/连接接口。
   0号机把`access_point.connection_name`设为`Hotspot`，EdgeLink会接管该连接名，
   并按产品模板统一SSID、共享地址和已交付的热点密码。
6. 备份NetworkManager连接配置、EasyTier配置和当前服务文件。
7. 灰度启用EdgeLink，先验证EasyTier单独恢复，再做错误Wi‑Fi回滚测试。
8. 最后进行真实外带：公司Wi‑Fi、手机热点、完全无网三个环境。

## 发布与回滚

- 安装器每次建立独立release目录并原子切换。
- `/etc/edgelink`不随卸载删除。
- 新服务启动失败会恢复安装前的`current`链接。
- 安装器只保留最近三个release目录。
- 发布包必须核对`dist/SHA256SUMS`。
- 设备验收通过前不要删除Reachy原有配网入口。

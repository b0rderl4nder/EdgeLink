# 新设备接入与0号机迁移

## 新设备标准流程

### 1. 基础检查

确认：

- systemd与NetworkManager运行。
- 无线接口名称及AP能力。
- Python版本不低于3.10。
- EasyTier已按正式配置安装为systemd实例。
- 设备时间正确。

先做只读预检：

```bash
sudo edgelinkctl preflight
```

首次安装默认只写入独立 release，不自动启用网络控制器。完成本机
NetworkManager、daemon 热点和 EasyTier 的边界核对后，才显式使用
`EDGELINK_START=1 sudo ./scripts/install.sh` 启用。已经在运行的设备升级时
保持原行为；若需要纯安装/检查，使用 `EDGELINK_START=0`。

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
- `access_point.connection_name`：只能是EdgeLink自有名称，例如
  `edgelink-provisioning`；如果设备存在Reachy daemon，
  `reserved_connection_names`必须明确填写daemon实际使用的热点配置名。
  该边界未声明时，预检会拒绝启用自动AP。
- `health.targets`：至少一个设备确实需要访问的稳定节点。
- `overlay.service_unit`：实际EasyTier unit。
- `overlay.expected_cidr_prefix`：本项目虚拟网段。
- `api.trusted_mutation_subnets`：只放本机与配网网段。

AP密码位于`/etc/edgelink/secrets.env`。未显式提供时使用公开初始密码`12345678`，禁止生成只留在设备内、管理员无法获知的随机密码；正式交付可通过权限受限的密码文件覆盖，并在首次联网后轮换。

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
4. 第一阶段只运行preflight和单次状态检查，不启用EdgeLink自动服务；安装时使用
   `EDGELINK_START=0`，不得依赖安装器的隐式启动。
5. EdgeLink和Reachy daemon必须使用不同的NetworkManager连接名。
   EdgeLink固定使用`edgelink-provisioning`；Reachy daemon的`Hotspot`保持原样，
   作为EdgeLink进程失效时仍能独立启动的物理兜底。禁止两个程序修改同一个连接配置。
6. 备份NetworkManager连接配置、EasyTier配置和当前服务文件。
7. 灰度启用EdgeLink，先验证EasyTier单独恢复，再做错误Wi‑Fi回滚测试。
   验收时必须分别启动`edgelink-provisioning`和`Hotspot`，确认两者的
   SSID、密码与连接UUID互不改写。
8. 最后进行真实外带：公司Wi‑Fi、手机热点、完全无网三个环境。

## 发布与回滚

- 安装器每次建立独立release目录并原子切换。
- `/etc/edgelink`不随卸载删除。
- 新服务启动失败会恢复安装前的`current`链接。
- 安装器只保留最近三个release目录。
- 发布包必须核对`dist/SHA256SUMS`。
- 设备验收通过前不要删除Reachy原有配网入口。

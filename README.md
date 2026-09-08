# EdgeLink Agent

面向机器人、树莓派和边缘计算设备的可复制网络恢复基础设施。它把Wi‑Fi配网、断网AP回退、网络健康判断和EasyTier恢复从业务App中剥离，确保业务程序升级或崩溃时设备仍可找回。

## 已实现

- 将无线关联、DHCP、默认路由、DNS、出口和EasyTier分层诊断。
- 启动及运行期间断网自动重连，达到阈值后启动配网AP。
- 新Wi‑Fi使用NetworkManager原生检查点事务切换；进程异常或验证失败自动回滚，旧式显式恢复与AP仍作为双重兜底。
- 重置复用Wi‑Fi配置中的旧静态地址、网关和DNS。
- Wi‑Fi正常而EasyTier异常时只恢复EasyTier，不错误启动AP。
- 手机优先的本地配网页面、JSON API和`edgelinkctl`。
- NetworkManager事件桥只唤醒单写入状态机，避免旧事件竞态。
- systemd常驻、部署前检查、原子版本切换和启动失败回滚。
- Wi‑Fi密码不进入命令行、响应或日志；NetworkManager密码文件用后即删。
- 零第三方运行依赖，支持Python 3.10及以上。

## 快速安装

目标系统要求Linux、systemd、NetworkManager、Python 3.10+；EasyTier为可选但默认启用。

新设备尚未安装EasyTier时，使用经过审核的官方归档和管理员配置进行离线注册：

```bash
sudo ./scripts/enroll_easytier.sh \
  easytier-linux-aarch64-v2.x.tar.gz device-default.conf default
```

脚本不会联网下载浮动版本，不会生成、读取或打印你的网络密钥；配置按`0600`安装。

```bash
tar -xzf edgelink-agent-<版本>.tar.gz
cd edgelink-agent-<版本>
sudo ./scripts/install.sh
sudo edgelinkctl preflight
edgelinkctl status
```

指定设备模板首次安装：

```bash
sudo env EDGELINK_CONFIG_SOURCE=deploy/examples/zebao0.json ./scripts/install.sh
```

首次安装在未提供密码时使用公开的初始配网密码`12345678`，不再生成管理员无法获知的随机值。正式交付可通过权限受限的`EDGELINK_AP_PASSWORD_FILE`覆盖，并应在首次联网后使用`sudo edgelink-set-ap-password`轮换。实际密码仅保存在`/etc/edgelink/secrets.env`，权限为`0600`，不进入安装日志；已安装设备升级时继续使用原密码。首次安装默认只完成release和预检，不自动启动网络控制器；确认daemon热点、无线接口、EasyTier unit和健康探测目标后，使用`EDGELINK_START=1 sudo ./scripts/install.sh`显式启用。已运行设备升级时会保持原有启用状态。

## 手机配网

设备失去可用上行网络达到设定阈值后会发出：

```text
Zebao-0
```

泽宝设备使用`Zebao-<编号>`，其他产品可在设备模板中配置自己的完整SSID；不暴露底层开发板品牌。连接热点后访问`http://10.42.0.1:8787/`。提交后页面先收到配网作业编号，Agent再切换无线网络。新网络失败时优先恢复原网络，否则每3秒重试AP，直到配网入口恢复。

健康检查按“链路、地址、路由、DNS、互联网证据、业务目标”分层记录。默认互联网模式接受NetworkManager完整连通、任一必需目标或DNS成功作为独立证据，避免单点探测故障误切热点；受控环境可关闭`accept_dns_as_internet_evidence`，只接受更严格的目标证据。仅需局域网的设备可将`minimum_connectivity`设为`local`。

## 本地命令

```bash
edgelinkctl status
edgelinkctl scan
edgelinkctl connect "现场WiFi"
edgelinkctl recover
edgelinkctl ap start
edgelinkctl ap stop
edgelinkctl preflight
```

修改类API默认只允许本机回环地址和配网AP网段访问。ZebaoApp应通过`127.0.0.1:8787`调用，不直接控制NetworkManager或EasyTier。

旧设备不知道原热点密码时，在设备本地终端执行`sudo edgelink-set-ap-password`安全设置新密码。输入不回显，密码不进入命令行、API或日志。

“恢复已保存网络”同样是无线接口级事务：程序先建立NetworkManager检查点，再切离当前热点并选择已保存网络；只有验证为真实非AP上行后才提交，失败会恢复原热点。有线接口和EasyTier不在该检查点范围内。

## 构建离线包

```bash
python3 tools/build_bundle.py
```

产物位于`dist/`，同时生成SHA‑256校验文件。详细设计和部署门禁见[架构](docs/architecture.md)及[设备接入](docs/device-onboarding.md)。

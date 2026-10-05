# 架构与状态机

## 责任边界

EdgeLink是设备级基础设施，不属于机器人场景、语音或管理端业务。业务App只读取状态和提交明确的配网操作。

```text
手机/本机CLI/ZebaoApp
          │
      Local API
          │
   EdgeLink Controller  ← NetworkManager dispatcher（只通知）
      │          │
NetworkManager  EasyTier systemd unit
      │          │
 Wi‑Fi/AP      虚拟网络
```

Controller是唯一自动写入者。dispatcher可能产生延迟或乱序事件，因此事件中不携带结论，只要求Controller重新读取当前真实状态。

## 健康分层

1. 无线链路是否关联。
2. DHCP是否获得IPv4。
3. 是否存在默认网关。
4. 是否获得且可使用DNS。
5. 配置的TCP目标是否可达（仅诊断）。
6. 指定Wi-Fi接口是否能完成证书校验HTTPS请求（互联网模式的准入证据）。
   若失败，还在同一接口上探测标准HTTP 204端点；重定向或登录页响应标为认证门户。
7. EasyTier进程是否运行。
8. 是否获得预期虚拟地址并存在有效Peer。

互联网模式需链路、地址、网关与第六层验证通过，且NetworkManager没有报告门户；DNS和TCP单独成功均不足以放行。局域网模式只要求链路、地址和网关，仍拒绝明确认证门户。EasyTier失败只进入overlay恢复，不触发Wi‑Fi/AP切换。

认证门户检查参照[NetworkManager的连通状态定义](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html)与[Android在中国网络使用的204端点](https://android.googlesource.com/platform/packages/modules/NetworkStack/+/a4af9b4846a3c10c6b0479a20b3e21361b7d64e5%5E%21/)。探测仅记录结果，不保存或上传门户页面内容。

## 状态

- `starting`：启动检查。
- `online`：上行和overlay均正常。
- `degraded`：上行存在但overlay或组件异常。
- `recovering`：重试已保存Wi‑Fi、DHCP和路由。
- `provisioning`：AP已启动，等待手机配网。
- `stopped`：服务停止。

## 事务式换网

1. HTTP只校验并受理作业，在切换网络前先向手机返回作业编号。
2. Controller单飞执行配网：未开始的任务只保留最新意图，执行中的任务不排队。
3. 按Wi‑Fi字节边界校验SSID与PSK，不记录密码。
4. 记录当前可用连接，暂停AP，创建独立的`edgelink-<SSID哈希>`连接配置。
5. 新配置保持`autoconnect=no`，强制恢复`ipv4.method=auto`并清理旧地址、网关和DNS，通过权限为`0600`的临时NetworkManager密码文件显式激活。
6. 验证地址、路由和绑定Wi-Fi的HTTPS证书与响应；全部通过后才提交`autoconnect=yes`。DNS和TCP目标仅用于定位故障。
7. 成功后恢复EasyTier；失败或进程中断时停用错误配置的自动连接并优先恢复原网络，原网络也不可用时才恢复AP。

NetworkManager的checkpoint机制是远程改网后自动回滚的官方基线；EdgeLink在不绑定特定D‑Bus客户端库的前提下，保留原连接名并显式验证回滚结果。配网页面遵循成熟门户的“广播—配网—失败后重新广播”流程。

EdgeLink只管理自己的`edgelink-provisioning`连接。产品daemon的热点配置必须保持独立，
不允许通过复用`connection_name`的方式“接管”，否则一个组件的错误会同时摧毁主路和兜底。

## 防抖与安全

- 普通断网按原宽限期处理；明确认证门户约5秒后退回AP，不重复尝试该SSID。已获得地址却无法验证互联网的SSID只在当前进程内排除自动重试，仍允许管理员手动重试或重新配网。
- AP动作和EasyTier重启分别有冷却时间。
- 密码不进入URL、命令行、状态、日志或诊断包。
- 配网类API只允许配置的可信网段；删除配置、停止AP和dispatcher事件仅允许本机回环地址。
- systemd限制文件系统写入、Home目录访问和Linux capabilities。
- 新版本先在独立release目录执行preflight，再原子切换`current`链接；启动失败恢复上一版。

## 适配扩展

`NetworkPort`和`OverlayPort`是稳定接口。后续可以增加：

- OpenWrt `ubus`网络适配器。
- 纯`wpa_supplicant`适配器。
- Tailscale、ZeroTier或WireGuard overlay适配器。
- 双无线网卡无缝AP模式。
- ModemManager蜂窝网络兜底。

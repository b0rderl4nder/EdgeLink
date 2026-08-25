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
5. 配置的TCP目标是否可达。
6. EasyTier进程是否运行。
7. 是否获得预期虚拟地址并存在有效Peer。

只有前五层同时满足才叫“上行可用”。EasyTier失败只进入overlay恢复，不触发Wi‑Fi/AP切换。

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
6. 验证地址、路由、DNS和目标；全部通过后才提交`autoconnect=yes`。
7. 成功后恢复EasyTier；失败或进程中断时停用错误配置的自动连接并优先恢复原网络，原网络也不可用时才恢复AP。

NetworkManager的checkpoint机制是远程改网后自动回滚的官方基线；EdgeLink在不绑定特定D‑Bus客户端库的前提下，保留原连接名并显式验证回滚结果。配网页面遵循成熟门户的“广播—配网—失败后重新广播”流程。

EdgeLink只管理自己的`edgelink-provisioning`连接。产品daemon的热点配置必须保持独立，
不允许通过复用`connection_name`的方式“接管”，否则一个组件的错误会同时摧毁主路和兜底。

## 防抖与安全

- 断网不会立即发出AP，先按间隔重试已保存网络。
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

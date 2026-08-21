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

1. 校验SSID和密码长度，不记录密码。
2. 暂停AP，创建独立的`edgelink-<SSID哈希>`连接配置。
3. 强制恢复`ipv4.method=auto`并清理旧地址、网关和DNS，避免同SSID热点复用公司网络参数。
4. 通过权限为`0600`的临时NetworkManager密码文件激活连接。
5. 验证地址、路由、DNS和目标。
6. 成功后恢复EasyTier；失败则重新启动AP。

已有产品热点可以通过`reuse_existing_profile`直接复用其NetworkManager连接，避免迁移时改变用户熟悉的SSID和密码。

## 防抖与安全

- 断网不会立即发出AP，先按间隔重试已保存网络。
- AP动作和EasyTier重启分别有冷却时间。
- 密码不进入URL、命令行、状态、日志或诊断包。
- API写操作仅允许配置的可信网段；默认是回环地址和AP网段。
- systemd限制文件系统写入、Home目录访问和Linux capabilities。
- 新版本先在独立release目录执行preflight，再原子切换`current`链接；启动失败恢复上一版。

## 适配扩展

`NetworkPort`和`OverlayPort`是稳定接口。后续可以增加：

- OpenWrt `ubus`网络适配器。
- 纯`wpa_supplicant`适配器。
- Tailscale、ZeroTier或WireGuard overlay适配器。
- 双无线网卡无缝AP模式。
- ModemManager蜂窝网络兜底。

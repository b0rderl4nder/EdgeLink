# 变更记录

## 0.1.0 — 2026-08-22

- 建立独立EdgeLink Agent，将设备网络生命周期从ZebaoApp和场景逻辑中剥离。
- 实现Wi‑Fi、DHCP、默认路由、DNS、出口和EasyTier分层健康判断。
- 实现启动与运行期断网重连、AP回退、配网活动租约和空闲后已保存网络重试。
- 实现事务式新Wi‑Fi连接；清理同SSID复用配置中的旧静态地址、网关和DNS，失败恢复AP。
- 支持复用Reachy现有`Hotspot`连接，不改变原`reachy-mini-ap`的SSID和密码。
- 实现EasyTier进程与Peer健康检查、失效启动和受控重启。
- 新增手机配网页面、本地API、CLI、NetworkManager dispatcher和systemd常驻服务。
- 新增部署前检查、原子release切换、20秒健康门禁、失败回滚及最近三版保留。
- 新增EasyTier离线注册脚本，要求固定官方构件和管理员审核配置，不下载浮动版本。
- 0号机完成AP切换与恢复、EasyTier停服自愈、dispatcher和双地址访问真机验收。


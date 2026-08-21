# 本地API

默认地址：`http://127.0.0.1:8787`；AP页面地址：`http://10.42.0.1:8787`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 服务与当前状态 |
| GET | `/v1/status` | 版本、来源提交、分层网络和overlay状态 |
| GET | `/v1/wifi/scan` | 附近Wi‑Fi列表 |
| POST | `/v1/wifi/connect` | 事务式连接并验证 |
| DELETE | `/v1/wifi/profiles/{name}` | 删除指定连接配置 |
| POST | `/v1/ap/start` | 显式启动AP |
| POST | `/v1/ap/stop` | 显式停止AP |
| POST | `/v1/network/recover` | 立即尝试已保存网络 |
| POST | `/v1/events/network` | NetworkManager事件唤醒 |
| POST | `/v1/provisioning/activity` | 手机实际操作时续期AP保持窗口 |

连接请求：

```json
{
  "ssid": "现场网络",
  "password": "不在响应和日志中回显"
}
```

状态响应只包含SSID、地址、健康分层、EasyTier虚拟地址、状态原因和计数，不包含任何密码或网络密钥。

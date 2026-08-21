# EdgeLink Agent工程规则

- 本仓库只维护通用边缘网络恢复程序，不包含ZebaoApp、Reachy daemon或EasyTier密钥。
- 系统写操作必须集中在`NetworkPort`与`OverlayPort`适配器，状态机保持单写入。
- dispatcher只发送唤醒事件，不执行恢复动作。
- 任何连接切换必须可回滚；任何真实断网测试必须先设置设备本地安全恢复任务。
- 密码、PSK、token、完整EasyTier配置不得进入日志、API、测试快照或Git。
- 新设备先执行preflight；已有热点控制器时必须确认协作边界。
- 发布前运行：

```bash
uv run --with pytest pytest -q
uv run --with ruff ruff check .
uv run --with mypy mypy edgelink_agent
uv run python -m compileall -q edgelink_agent tests tools
uv run python tools/build_bundle.py
```

- 小版本本地提交；中版本经过真机验收后才配置并推送独立远端仓库。


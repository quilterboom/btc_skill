# deployed/

系统服务 / 定时任务 / 凭证路径等"运行时配置"的版本化镜像。

## 为什么需要这个目录

`/root/.config/systemd/user/*.service` 这类文件**不在 git 里**，
git 只能管代码，不能管"服务当前跑的是什么参数"。
一旦代码提交和服务配置不同步，下次 session 的人
（包括被 daily reset 清掉上下文的我自己）会一脸懵：
"代码这么跑，服务是哪个版本？"

## 这里管什么

| 文件 | 镜像的目标 |
|---|---|
| `btc-watch.service` | `/root/.config/systemd/user/btc-watch.service` |

将来需要再加的：
- `btc-status-web.service`（3003 端口面板）
- cron 触发的指令模板（每个 .service ExecStart 的 inline 命令）
- Telegram 凭证路径（不要写真 token，只记路径）

## 怎么用

1. 改了 service 文件 → 同步更新本目录的镜像
2. 改了代码（scripts/）→ 同步打 git tag 或在 commit message 里写明服务是否需要 reload
3. 出问题时先看本目录的版本，再看 `/root/data/watch.log`

# 执行权限守卫（astrbot_plugin_exec_guard）

只在白名单会话里放行「本机执行 / 文件读写 / 浏览器 / 桌面控制」这一类工具；
其它会话（群聊、陌生人）直接把工具从这一轮的可用列表里摘掉。

## 为什么要有它

AstrBot 的 `computer_use_local_permissions` 只按**角色**分（admin / member），
但「群里的 admin」和「主人私聊里的 admin」是同一个角色 —— 群友只要被认成管理员，
就能让它执行代码。这个插件把闸门下沉到**会话**这一层。

## 怎么判断

`on_llm_request` 时依次看：

1. `enabled=false` → 直接放行（等于没装）
2. 会话角色是 admin 且 `allow_admin=true` → 放行
3. `unified_msg_origin` 里包含 `allow_patterns` 任意一项 → 放行
4. 否则从 `req.func_tool` 里逐个 `remove_tool()` 掉 `blocked_tools` 里的工具

被摘掉的工具在这一轮里执行器查不到名字，等于彻底挡住；
日志会打 `[exec_guard] 会话 … 已移除 N 个本机工具: …`。

## 命令

`/execguard`：显示开关、当前会话、是否放行、白名单、受限工具数量。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| allow_admin | true | 管理员会话放行 |
| allow_patterns | `["FriendMessage:123456789"]` | 会话 umo 子串匹配，命中即放行 |
| blocked_tools | 17 项 | 要摘掉的工具名 |

## 已知边界

- 只挡「模型调用工具」这一步，不改变底层权限策略（member 仍能从别的路径执行）
- 群里用 `future_task` 建的定时任务绑定的是群会话，将来执行时同样被挡 —— 这是有意的
- 工具名要跟 `GET /api/v1/tools` 对得上；AstrBot 升级改了工具名的话需要同步改 `blocked_tools`

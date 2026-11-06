# 更新日志

## v1.2.0 — 2026-11-06 00:27
- 兴趣点新增 `keywords`（2-4 个 B 站搜索词），挑视频时先按它搜，搜不到再退回分区排行
- `next_pick()` 改成**兴趣点分区优先**、每第三条插一个探索/热门/DeepSeek，不再让主力预算排到最后
- 修 `remaining_videos()` 读错键（读 `zones` 而状态里是 `budget`），导致「今天还剩 N 个额度」长期偏小

## v1.1.0
- 新增探索位（`explore_count` / `explore_pool`）
- 新增热门位（`hot_enabled` / `hot_count`）
- 新增 DeepSeek 位（`deepseek_keywords` / `deepseek_max_per_day`）
- 面板加「最近 7 天」汇总与分区绿条

## v1.0.0
- 首版：按人格/心情/日记生成兴趣点，按权重分配分区预算（至少 3 个分区）
- 提供 `state` / `roll` / `used` / `history` 四个接口和 `/interest` 命令

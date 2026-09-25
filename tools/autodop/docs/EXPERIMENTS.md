# 实验日志导航

原始 `runs/` 约 39 GB，保持原路径，不改写历史数据、失败记录和模型来源。`EXPERIMENT_INDEX.json` 索引各实验顶层报告和状态文件并记录 SHA-256；它不是全部原始日志的备份。

`reports/` 是可随 Git 和发布包分发的报告快照，来源见 `reports/SOURCES.json`。快照内部的本机绝对路径与相对链接仍按原实验目录解释，未打包原始数据的链接不能离线打开。

## 主要报告

- [runs/report90_20260912/FINAL_REPORT_EXTENDED.md](reports/report90_20260912/FINAL_REPORT_EXTENDED.md)
- [runs/report90_20260912/FINAL_DELIVERY.json](reports/report90_20260912/FINAL_DELIVERY.json)
- [runs/report90_20260912/PROTOCOL.json](reports/report90_20260912/PROTOCOL.json)
- [runs/report90_20260912/MODEL_ACCEPTANCE.md](reports/report90_20260912/MODEL_ACCEPTANCE.md)
- [runs/report90_20260912/PAPER_EXPERIMENT_SCOPE.md](reports/report90_20260912/PAPER_EXPERIMENT_SCOPE.md)
- [runs/report90_20260912/distillation_concurrency_extension/REPORT.md](reports/report90_20260912/distillation_concurrency_extension/REPORT.md)
- [runs/preference_random200_20260911/random200/positive90/REPORT.md](reports/preference_random200_20260911/random200/positive90/REPORT.md)
- [runs/ablation_cost_20260912/REPORT.md](reports/ablation_cost_20260912/REPORT.md)

90 条查询来自已测 200 条查询的事后正收益筛选，不能据此声称独立泛化或所有工作负载收益。有限窗口的并发测试不构成长期吞吐保证。验收范围和未完成项以各报告原文为准。

## 9 月 19–20 日追加实验

- [六模型对照](reports/report90_20260912/six_model_control_20260919/full/UNIFIED_REPORT.md)
- [四种基础模型对照](reports/report90_20260912/basic_four_control_20260920/full/UNIFIED_REPORT.md)

同目录附原始 STATUS 和 FINAL_AUDIT 快照；索引还包含 182 个 report90 子实验的报告入口。

## 原始实验目录

以下路径相对 `tools/autodop/`，仅在保留了本机实验数据时可用。

- `runs/ablation_cost_20260912/`：2 个顶层记录
- `runs/acceptance_preservation_policy03_20260910/`：1 个顶层记录
- `runs/broad_analytical_20260907/`：2 个顶层记录
- `runs/calibrated_structure_evaluation01_20260910/`：0 个顶层记录
- `runs/composable_abd_batch09_20260909/`：0 个顶层记录
- `runs/composable_abd_double_left_batch29_20260909/`：0 个顶层记录
- `runs/composable_abd_double_union_batch38_20260909/`：0 个顶层记录
- `runs/composable_abd_full_join_batch32_20260909/`：0 个顶层记录
- `runs/composable_abd_full_outer_batch33_20260909/`：0 个顶层记录
- `runs/composable_abd_full_union_batch34_20260909/`：0 个顶层记录
- `runs/composable_abd_inner_batch24_20260909/`：0 个顶层记录
- `runs/composable_abd_inner_full_batch26_20260909/`：0 个顶层记录
- `runs/composable_abd_inner_left_batch25_20260909/`：0 个顶层记录
- `runs/composable_abd_inner_union_batch27_20260909/`：0 个顶层记录
- `runs/composable_abd_left_full_batch30_20260909/`：0 个顶层记录
- `runs/composable_abd_left_inner_batch28_20260909/`：0 个顶层记录
- `runs/composable_abd_union_full_batch31_20260909/`：0 个顶层记录
- `runs/composable_abd_union_nesting_batch35_20260909/`：0 个顶层记录
- `runs/composable_abd_union_outer_batch37_20260909/`：0 个顶层记录
- `runs/composable_abd_union_right_batch36_20260909/`：0 个顶层记录
- `runs/composable_ac_binary_batch22_20260909/`：0 个顶层记录
- `runs/composable_acd_double_full_batch49_20260909/`：0 个顶层记录
- `runs/composable_acd_double_left_batch44_20260909/`：0 个顶层记录
- `runs/composable_acd_double_union_batch54_20260909/`：0 个顶层记录
- `runs/composable_acd_full_inner_batch47_20260909/`：0 个顶层记录
- `runs/composable_acd_full_left_batch48_20260909/`：0 个顶层记录
- `runs/composable_acd_full_union_batch50_20260909/`：0 个顶层记录
- `runs/composable_acd_inner_batch39_20260909/`：0 个顶层记录
- `runs/composable_acd_inner_full_batch41_20260909/`：0 个顶层记录
- `runs/composable_acd_inner_left_batch40_20260909/`：0 个顶层记录
- `runs/composable_acd_inner_union_batch42_20260909/`：0 个顶层记录
- `runs/composable_acd_left_full_batch45_20260909/`：0 个顶层记录
- `runs/composable_acd_left_inner_batch43_20260909/`：0 个顶层记录
- `runs/composable_acd_left_union_batch46_20260909/`：0 个顶层记录
- `runs/composable_acd_union_full_batch53_20260909/`：0 个顶层记录
- `runs/composable_acd_union_inner_batch51_20260909/`：0 个顶层记录
- `runs/composable_acd_union_left_batch52_20260909/`：0 个顶层记录
- `runs/composable_ad_binary_batch23_20260909/`：0 个顶层记录
- `runs/composable_binary_ab_batch08_20260909/`：0 个顶层记录
- `runs/composable_branch_grammar_20260909/`：0 个顶层记录
- `runs/composable_double_full_batch18_20260909/`：0 个顶层记录
- `runs/composable_double_union_batch21_20260909/`：0 个顶层记录
- `runs/composable_full_batch03_20260909/`：0 个顶层记录
- `runs/composable_full_right_batch17_20260909/`：0 个顶层记录
- `runs/composable_high_pair_batch06_20260909/`：0 个顶层记录
- `runs/composable_inner_batch04_20260909/`：0 个顶层记录
- `runs/composable_inner_full_batch12_20260909/`：0 个顶层记录
- `runs/composable_join_chain_batch05_20260909/`：0 个顶层记录
- `runs/composable_left_chain_batch14_20260909/`：0 个顶层记录
- `runs/composable_left_outer_batch15_20260909/`：0 个顶层记录
- `runs/composable_mid_pair_batch07_20260909/`：0 个顶层记录
- `runs/composable_nested_batch02_20260909/`：0 个顶层记录
- `runs/composable_pure_join_batch10_20260909/`：0 个顶层记录
- `runs/composable_right_join_batch11_20260909/`：0 个顶层记录
- `runs/composable_union_full_batch16_20260909/`：0 个顶层记录
- `runs/composable_union_join_batch13_20260909/`：0 个顶层记录
- `runs/composable_union_nesting_batch19_20260909/`：0 个顶层记录
- `runs/composable_union_outer_batch20_20260909/`：0 个顶层记录
- `runs/date_partition_layout_20260908/`：0 个顶层记录
- `runs/fragment_vs_query_20260907/`：0 个顶层记录
- `runs/frozen_structure_generalization01_20260910/`：1 个顶层记录
- `runs/frozen_structure_transfer04_20260911/`：0 个顶层记录
- `runs/independent_window_candidates01_20260910/`：0 个顶层记录
- `runs/inventory_targeted_training_20260908/`：0 个顶层记录
- `runs/legacy_branch_development01_20260909/`：0 个顶层记录
- `runs/legacy_cost_repair01_20260909/`：0 个顶层记录
- `runs/legacy_cost_repair02_20260909/`：0 个顶层记录
- `runs/legacy_cost_repair03_20260909/`：1 个顶层记录
- `runs/legacy_cost_repair04_20260910/`：0 个顶层记录
- `runs/legacy_policy_repair02_20260910/`：1 个顶层记录
- `runs/multi_low_branch_expansion01_20260909/`：0 个顶层记录
- `runs/multi_low_cost_repair01_20260909/`：0 个顶层记录
- `runs/multi_low_cost_repair02_20260909/`：0 个顶层记录
- `runs/multi_low_cost_repair03_20260909/`：0 个顶层记录
- `runs/multi_low_coverage_cost01_20260910/`：2 个顶层记录
- `runs/multi_low_coverage_cost02_20260910/`：2 个顶层记录
- `runs/multi_low_coverage_cost03_20260910/`：2 个顶层记录
- `runs/multi_low_coverage_development01_20260910/`：1 个顶层记录
- `runs/multi_low_development01_20260909/`：1 个顶层记录
- `runs/multi_low_policy_repair04_20260909/`：0 个顶层记录
- `runs/multi_low_retention_cost04_20260910/`：2 个顶层记录
- `runs/multi_structure_feedback_cost05_20260910/`：2 个顶层记录
- `runs/novel_structure_development01_20260910/`：1 个顶层记录
- `runs/novel_structure_policy01_20260910/`：0 个顶层记录
- `runs/paper_campaign_20260905/`：0 个顶层记录
- `runs/preference_random200_20260911/`：4 个顶层记录
- `runs/range_branch_compositions01_20260909/`：0 个顶层记录
- `runs/range_coverage_cost01_20260910/`：2 个顶层记录
- `runs/range_coverage_cost02_20260910/`：2 个顶层记录
- `runs/range_coverage_development01_20260910/`：0 个顶层记录
- `runs/range_coverage_policy01_20260910/`：0 个顶层记录
- `runs/range_coverage_policy02_20260910/`：0 个顶层记录
- `runs/range_embedding_repair03_20260909/`：0 个顶层记录
- `runs/range_model_development01_20260909/`：0 个顶层记录
- `runs/range_model_validation01_20260909/`：0 个顶层记录
- `runs/range_policy_embedding_repair07_20260909/`：0 个顶层记录
- `runs/range_temporal_transfer01_20260909/`：0 个顶层记录
- `runs/range_topology_expansion01_20260909/`：0 个顶层记录
- `runs/report90_20260912/`：15 个顶层记录
- `runs/statistics_repair_20260908/`：0 个顶层记录
- `runs/structural_scale_development01_20260911/`：3 个顶层记录
- `runs/supplier_network_balanced_cost_20260908/`：0 个顶层记录
- `runs/uniform_teacher_policy01_20260910/`：2 个顶层记录
- `runs/uniform_teacher_policy02_budget5000_20260910/`：2 个顶层记录

历史批次脚本已归档，恢复方式见 [archive/README.md](../archive/README.md)。早期设计和修复记录见 [history/](history/README.md)。

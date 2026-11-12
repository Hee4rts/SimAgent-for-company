# 已注册工具

工具：`analyze_approval_queues`\
作用：用 Erlang-C 和 Little 定律分析独立审批人员池的负荷、等待达标率和平均积压。

工具：`calculate_leave_route`\
作用：根据已核算的请假事实确定审批路径、命中理由和通知分支；缺失事实返回 needs_data。

工具：`compare_approval_scenarios`\
作用：比较同一申请群体和观察口径的仿真指标，返回时长、SLA、完成率和成本差异及可选两因素归因。

工具：`get_leave_workflow`\
作用：读取内置请假审批流程快照，返回节点、分支条件、审批角色、退回目标和来源版本。

工具：`optimize_approval_capacity`\
作用：在预算和时效目标下选择合格候选人员子集，用整数差分进化或有界枚举搜索并用独立种子复核。

工具：`resolve_leave_approvers`\
作用：从已解析的组织角色快照匹配审批人，应用有效授权、去重并排除申请人自己。

工具：`simulate_leave_workflow`\
作用：仿真请假审批的共享人员、工作日历、或签领取、自动跳过、转交和退回重提，输出时效与成本。

工具：`web_search`\
作用：通过 SerpApi 搜索互联网公开信息，适用于需要最新资料或来源链接的问题。


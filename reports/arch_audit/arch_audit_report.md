# 全仓代码架构与静默通过审计

**工作树核验日　2026-11-24（新加坡）**  
**基线提交　`917f14b034430fadad9d8680a61da3f1bd8987c4`，含未提交的本地代码与结果。**

## 结论

在任务开始时的 22,889 个文件中，确认 **32 项静默通过、静默降级或验证盲点，其中 P0 4 项、P1 20 项、P2 8 项**。P0 按根因计数，同一缺陷在多个入口出现只计一次。当前引擎字节正确，已存生产回执的八张输入表和两份补充证据的 SHA-256 均与本地文件匹配。以下缺陷说明失效条件，不能据此判定那次真实回放失败。

| 优先处置 | 发生什么 | 当前证据 |
|---|---|---|
| S01 正式回放的冻结检查 | 检查哪些文件也由待验锁自己决定；漏列项目可通过 | 空锁表达式在内存中通过；独立完整性命令能拒绝错误锁 |
| S02 采集阻断回执 | 回执缺失、字段缺失都被解释为“无阻断”；留存旧行情的来源冲突可能因此消失 | 正式入口控制流已定位；当前回执完整，阻断列表为空 |
| S03 Phase 2B-P1 汇总 | BASE 锚点失败仍给出正常退出码和可消费的排名 | 单行、漂移输入实调 main，得到 false、OK、WARN 与正常返回 |
| S04 研究签发 | 解释器优化会一并删除“禁止优化”的守卫、冻结断言和若干对账断言 | 对原守卫单独编译，优化后没有检查指令；P1 导出器另有显式保护 |

交易成本底层有 **2 份正式实现**，日级交易账本有 **5 份执行循环**。近期研究的最高耦合位于 P1/P2 对哑铃函数源码做字符串替换。适合先修验收入口并锁定几本可人工核算的小账本，再迁移研究侧的公共逻辑。工程去重有精确上限：既有报告目录中 **136 份重复 CSV、10,923,948 B（10.42 MiB）**，另有可单独处理的字节码。历史回执路径和数据身份需要保留。

## 范围、方法与证据强度

- 盘点覆盖隐藏工程目录、未跟踪的研究脚本、Git 忽略的本地行情和报告树内的程序。基线共 1,328,517,280 B，含 39 个 Python、6 个 JS、2 个 CJS、33 个 JSON、31 个 Markdown、1 个 MDC 和 1 个 CI YAML；项目代码区 PowerShell 脚本为 0。另对审计期间出现的 9 个文件做了补扫，见末节。清单共 22,898 行数据。
- Python 逐文件解析 AST，提取异常处理、all/get/getattr、断言与导入。JS/CJS 搜索 catch、every、空值合并、布尔及比较，JSON 全部解析并定位状态字面量。沿调用方、生产者、现存回执和反例判断可达性。HTML 单独检查构建链：现页包含 1 个数据脚本和 6 个源码脚本，后六段逐一与当前 JS 文本相等。
- 主快照得到 72 处异常处理候选行、95 处 all/every 行、428 处取值/默认行、164 处通过字面量或真值标记、240 处断言行。按“每文件中不同的物理行”计数；长 JS 行可含多个表达式。`file_inventory.csv` 可按文件、检查方式和候选行复核。`silent_pass_findings.csv` 提供每项的主行、原文、关联代码、可达条件、现场证据和回归建议。
- `.git/` 和 `.venv/` 属于版本库内部及本地环境，不计入工程规模。对 `data/fetch_cache/`、字节码、根凭据文件盘点文件名和大小，并以修改时间复核写入边界；清单不保存凭据内容。大报告 JSON 解析结构、身份字段和有针对性的证据，未把其中每个历史 PASS 视作本次验证。
- **P0** 意味着正式冻结保护、正式生产门禁或命名为验收的入口可以把失败当成成功。**P1** 会改变准入、账本解释、核心指标或承担重大维护风险。**P2** 主要影响反馈、诊断完整度、文档或局部维护。风险等级与本地已经出现该错误的概率独立。K/E/D 项分属架构、工程、文档，引用 S 项不重复累计漏洞数。

## 静默通过与退化路径

下列“现状”区分了本次内存反例、可达的静态分支和仓库已有记录。除 S03 的内存场景外，没有运行会覆盖研究结果的失败路径。

### S01 · P0 · 冻结核验的范围来自待验锁本身

锁为空或漏列文件时，actual 与 expected 可以相等。同步变更锁和引擎也能过这一关。独立完整性命令会拒绝，正式入口没有调用它。 同一遗漏还见于早期去单票压测。

[run.py:538](../../run.py#L538)　` actual={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in expected} `

**证据与现状**　内存求值：空锁得到空 actual，比较为真。当前五个批准哈希全部通过。 **回归要求**　共用带批准键集的只读检查；测试漏一项、额外一项及锁值漂移均阻断正式回放。

关联位置　[run.py:537](../../run.py#L537)、[run.py:539](../../run.py#L539)、[run.py:550](../../run.py#L550)、[scripts/check_integrity.py:21](../../scripts/check_integrity.py#L21)、[scripts/check_integrity.py:24](../../scripts/check_integrity.py#L24)、[research/exp_drop_300308_stress.py:352](../../research/exp_drop_300308_stress.py#L352)。

### S02 · P0 · 缺采集回执或 blockers 键会抹去阻断状态

若采集器保留旧的合法行情并在回执记录覆盖冲突，CSV 可独立通过 prepare。回执遗失或字段改名后，正式分支收到空列表，双端行情一致也不能还原来源冲突。

[run.py:543](../../run.py#L543)　` blockers=fetch_audit.get('blockers',[]) `

**证据与现状**　条件路径。当前本地回执存在，blockers=[]。有独立人工底稿的历史导入应有明确来源模式。 **回归要求**　按采集批次绑定回执与输入哈希；缺证据为未核验。保留可识别的人工导入协议。

关联位置　[run.py:542](../../run.py#L542)、[run.py:550](../../run.py#L550)、[run.py:571](../../run.py#L571)、[fetch_v4_data.py:905](../../fetch_v4_data.py#L905)、[fetch_v4_data.py:1052](../../fetch_v4_data.py#L1052)、[scripts/prepare_workspace.py:25](../../scripts/prepare_workspace.py#L25)、[offline/app.js:52](../../offline/app.js#L52)。

### S03 · P0 · 锚点失败仍打印 OK 并以 0 结束

锚点表能准确标记 MISSING/DRIFT，失败布尔只用于 stderr 提示。消费进程状态的自动化会接收一份验收失败的报告。

[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:380](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L380)　` print("[WARN] Some BASE anchor checks did NOT pass! Inspect p1_baseline_anchor_check.csv.", file=sys.stderr) `

**证据与现状**　内存运行实际 main，保留一个 IS 行并改动净收益，摘要 false、打印 OK 和 WARN，正常返回 None。现存摘要字段为 true。 **回归要求**　先写失败证据，再返回非零；单个锚点漂移应有独立 CLI 负例。

关联位置　[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:225](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L225)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:336](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L336)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:378](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L378)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:384](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L384)。

### S04 · P0 · 禁止优化的保护本身写成 assert

在优化解释器中，入口的版本检查和随后的 assert 同时消失。若其余计算完成，研究入口仍能落盘。正式 run 的 Node 校验及 P2 的显式 check 有不同的保护。

[research/exp_50_pool_topk_diag.py:362](../../research/exp_50_pool_topk_diag.py#L362)　` assert sys.version_info[:2]==(3,11) and not sys.flags.optimize `

**证据与现状**　只编译真实入口守卫，optimize=1 后无检查字节码。没有在优化模式运行研究或声称历史产物错误。 **回归要求**　普通 if/raise 在进入研究入口时检查 3.11 和 __debug__；把决定签发的关键断言移到显式检查。

关联位置　[research/exp_50_pool_topk_diag.py:44](../../research/exp_50_pool_topk_diag.py#L44)、[research/exp_50_pool_topk_diag.py:51](../../research/exp_50_pool_topk_diag.py#L51)、[research/exp_50_pool_barbell.py:462](../../research/exp_50_pool_barbell.py#L462)、[research/factor_lab/run_phase2a.py:109](../../research/factor_lab/run_phase2a.py#L109)、[research/factor_lab/run_phase2a_plus.py:173](../../research/factor_lab/run_phase2a_plus.py#L173)、[scripts/run_phase2b_p1_ablation.py:407](../../scripts/run_phase2b_p1_ablation.py#L407)。

### S05 · P1 · 空证据清单可通过快速复用

行情等八张表能匹配，但旧报告缺 evidence_sha256 或其为 {} 时，补丁和日历前瞻证据完全跳过，直接打印零问题。快速路径也没有绑定 policy、crosscheck 或本次回执。

[fetch_v4_data.py:833](../../fetch_v4_data.py#L833)　` for name,digest in approved.get('evidence_sha256',{}).items())): `

**证据与现状**　内存执行原 all 表达式，缺键得到 True。本地报告有两项证据并已逐项复核。 **回归要求**　按快照协议声明必需键和可选证据存在性；身份缺失降级到完整校验。

关联位置　[fetch_v4_data.py:574](../../fetch_v4_data.py#L574)、[fetch_v4_data.py:829](../../fetch_v4_data.py#L829)、[fetch_v4_data.py:835](../../fetch_v4_data.py#L835)、[run.py:560](../../run.py#L560)。

### S06 · P1 · 清单自行决定必须验证哪些输入

删去某项 SHA 或给空映射，逐项检查仍成功。P1 附加文件还可因为不存在而不进清单。后续 prepare、历史摘要及日历长度是额外保护，但不能证明未列输入的身份。

[research/exp_50_pool_topk_diag.py:53](../../research/exp_50_pool_topk_diag.py#L53)　` for k,digest in audit['sha256'].items()) `

**证据与现状**　当前正式审计八张 CSV 身份齐全。 **回归要求**　先比较期望与实到键集，再比每项字节；标注必需/可选及缺失原因。

关联位置　[research/exp_50_pool_topk_diag.py:51](../../research/exp_50_pool_topk_diag.py#L51)、[research/exp_50_pool_topk_diag.py:55](../../research/exp_50_pool_topk_diag.py#L55)、[scripts/run_phase2b_p1_ablation.py:151](../../scripts/run_phase2b_p1_ablation.py#L151)、[scripts/run_phase2b_p1_ablation.py:155](../../scripts/run_phase2b_p1_ablation.py#L155)、[scripts/run_phase2b_p1_ablation.py:164](../../scripts/run_phase2b_p1_ablation.py#L164)、[research/exp_50_pool_v5_precheck.py:204](../../research/exp_50_pool_v5_precheck.py#L204)。

### S07 · P1 · 锁定候选与配股负对照均可被空集合跳过

locked_candidates 拼错后退到 {}；screen_only 存在但为空时，最后的负对照全称命题也为真。若配额和相关性均合格，封池研究报告可显示 ready_to_apply。

[research/final_pool_screen.py:362](../../research/final_pool_screen.py#L362)　` locks_met = all(code in by_group.get(int(group), {}).get('selected', []) for group, code in locked.items()) `

**证据与现状**　当前配置存在锁定候选和负对照。 **回归要求**　校验配置键、版本和预期对照标识；逐个输出锁定与拒绝证据。

关联位置　[research/final_pool_screen.py:308](../../research/final_pool_screen.py#L308)、[research/final_pool_screen.py:361](../../research/final_pool_screen.py#L361)、[research/final_pool_screen.py:366](../../research/final_pool_screen.py#L366)、[research/universe_50_final_config.json:15](../../research/universe_50_final_config.json#L15)。

### S08 · P1 · 零个股票池也能得出 ready 和两个 F3 准入模式

空结构和空模式字典的 all 均为真；只传一个池也不会检查另一池。正常 main 明确构造两个池，风险位于可复用的决策函数。

[research/exp_50_pool_phase2b.py:267](../../research/exp_50_pool_phase2b.py#L267)　` passed=all(all(v['checks'].values()) for v in structural.values()) `

**证据与现状**　内存实调用 summarize_pools({}) 得到 ready=True、F3/S 和 F3/V。现有负例覆盖有数据的弱池。 **回归要求**　函数入口核验两池、完整场景与滚动窗；加入零池、缺一池负例。

关联位置　[research/exp_50_pool_phase2b.py:226](../../research/exp_50_pool_phase2b.py#L226)、[research/exp_50_pool_phase2b.py:270](../../research/exp_50_pool_phase2b.py#L270)、[research/exp_50_pool_phase2b.py:420](../../research/exp_50_pool_phase2b.py#L420)、[tests/test_phase2b.py:55](../../tests/test_phase2b.py#L55)。

### S09 · P1 · 空正交性证据能拿到 PASS

空字典在 any 检查为假，又在 all 检查为真。正常生产者生成 W1、OOS 两行；若使用残缺结果，准入函数没有独立核验这两窗。

[research/factor_lab/court.py:239](../../research/factor_lab/court.py#L239)　` 'PASS' if all(x is not None for x in rhos) else 'CONDITIONAL') `

**证据与现状**　静态，正常生成器无样本会写 rho=None，走 CONDITIONAL。 **回归要求**　显式核验两窗及有效样本数；用 {}、缺一窗、rho=None 区分缺证与拒绝。

关联位置　[research/factor_lab/court.py:90](../../research/factor_lab/court.py#L90)、[research/factor_lab/court.py:237](../../research/factor_lab/court.py#L237)、[research/factor_lab/court.py:264](../../research/factor_lab/court.py#L264)。

### S10 · P1 · 使用旧校准时只读日期和总布尔

三股报告存有逐股结果及基线散列；准入消费者只检查冻结日和 calibration_passed。旧报告或缺失逐股明细仍可能放开新标的采集。

[fetch_v4_data.py:520](../../fetch_v4_data.py#L520)　` if calibration.get('asof') != freeze or calibration.get('calibration_passed') is not True: `

**证据与现状**　静态；来源协议另有检查，正式采集后还有输入校验。 **回归要求**　绑定三股实际校准输入切片、覆盖与规则版本，避免扩池使无关行变化造成误阻断。

关联位置　[research/verify_tushare_baseline.py:183](../../research/verify_tushare_baseline.py#L183)、[research/verify_tushare_baseline.py:188](../../research/verify_tushare_baseline.py#L188)、[fetch_v4_data.py:845](../../fetch_v4_data.py#L845)。

### S11 · P1 · 尾部非周五的完整周标记可自证

当行是截断后的最后一行且当天非周五，expected 直接采信该行 week_end=1。两端写法相同，即使已知下一交易日仍在本周，错误尾标也能进入周线。

[inputs.py:198](../../inputs.py#L198)　` if i+1<len(days) else d.weekday()==4 or r['week_end']=='1') `

**证据与现状**　内存求值：周三尾行填 1 得 True。本地正式冻结日是周五，节前短周确实可合法结束。 **回归要求**　用独立下一交易日或已核验的短周日历判别；周三后周四开市与节前周三后下周开市分别测试。

关联位置　[inputs.py:199](../../inputs.py#L199)、[inputs.py:190](../../inputs.py#L190)、[offline/input.js:117](../../offline/input.js#L117)、[engine.py:54](../../engine.py#L54)、[fetch_v4_data.py:103](../../fetch_v4_data.py#L103)。

### S12 · P1 · 缺失或重复 IS 单元仍计算最差卡玛

聚合只按已有组计算，重复行改变均值，缺少低分单元抬高最差值，锚点只检查首行。独立调用此 evaluator 时，导出器的全矩阵检查没有被重验。

[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:254](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L254)　` for var, g in is_core.groupby("variant", sort=False): `

**证据与现状**　S03 的内存运行以一行也生成排名，当前导出器另有 216 行键集检查。 **回归要求**　按协议对变体×池×窗×场景做唯一且全覆盖检查；有缺项时输出不可排名。

关联位置　[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:234](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L234)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:264](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L264)、[scripts/run_phase2b_p1_ablation.py:510](../../scripts/run_phase2b_p1_ablation.py#L510)。

### S13 · P1 · 缺失或非数值费用被计为零

佣金、税费、滑点缺列或无法解析时生成 0，total_cost 坏值同样清零，导致汇总低估摩擦。

[scripts/audit_kt2_turnover_t2_attribution.py:150](../../scripts/audit_kt2_turnover_t2_attribution.py#L150)　` df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0 `

**证据与现状**　影响归因表。冻结交易引擎实际扣费另有完整公式。 **回归要求**　验证费项及总额加和，只有契约定义的免征项可入账为 0。

关联位置　[scripts/audit_kt2_turnover_t2_attribution.py:153](../../scripts/audit_kt2_turnover_t2_attribution.py#L153)、[scripts/audit_kt2_turnover_t2_attribution.py:158](../../scripts/audit_kt2_turnover_t2_attribution.py#L158)。

### S14 · P1 · 信号匹配成功即可配对，无交易顺序检查

精确键分支没有核验 T+2 在 T+1 之后或二者可比，切片只检查 T+1 日期。历史复核记录一笔反向 455 个市场日的配对，计入 −3.55 个百分点。

[scripts/audit_kt2_turnover_t2_attribution.py:399](../../scripts/audit_kt2_turnover_t2_attribution.py#L399)　` on=["signal_date", "sleeve", "symbol", "side", "_occ"], `

**证据与现状**　已有复核记录，非本次重演。 **回归要求**　分别记录同一信号/延迟/跨窗/跨权益；对不能配对的保留未匹配状态。

关联位置　[scripts/audit_kt2_turnover_t2_attribution.py:394](../../scripts/audit_kt2_turnover_t2_attribution.py#L394)、[scripts/audit_kt2_turnover_t2_attribution.py:527](../../scripts/audit_kt2_turnover_t2_attribution.py#L527)、[reports/phase2b_p1_attribution/attribution_crosscheck.md:17](../../reports/phase2b_p1_attribution/attribution_crosscheck.md#L17)。

### S15 · P1 · 风控执行与预期又调用同一决策函数

第二次调用同一 decide 可查账本接线或错误净值来源，规则写反时两边可一起错。现有 DD 边界断言、诊断行情及 BASE 锚点能查部分独立条件。

[research/exp_50_pool_phase2b_p2.py:169](../../research/exp_50_pool_phase2b_p2.py#L169)　` weak, cap = decide(day, nav, peak, weak) `

**证据与现状**　静态；不是对全部 36 个前缀证明的否定。 **回归要求**　为每种弱/强状态固定少量外生输入和预期预算，单独覆盖状态维持与恢复。

关联位置　[research/exp_50_pool_phase2b_p2.py:185](../../research/exp_50_pool_phase2b_p2.py#L185)、[research/exp_50_pool_phase2b_p2.py:188](../../research/exp_50_pool_phase2b_p2.py#L188)、[research/exp_50_pool_phase2b_p2.py:559](../../research/exp_50_pool_phase2b_p2.py#L559)。

### S16 · P1 · 手工列名拼错会退回自动识别

显式映射的源列不存在时静默略过，可能选错别名或走费用清零。窗口键拼错会新增窗口并保留原窗口。

[scripts/audit_kt2_turnover_t2_attribution.py:89](../../scripts/audit_kt2_turnover_t2_attribution.py#L89)　` if raw_col in df.columns: `

**证据与现状**　静态；当前归因底稿另有人工交叉检查。 **回归要求**　约束合法目标列和窗口名；拒绝未消耗的覆盖键，保留最终映射。

关联位置　[scripts/audit_kt2_turnover_t2_attribution.py:92](../../scripts/audit_kt2_turnover_t2_attribution.py#L92)、[scripts/audit_kt2_turnover_t2_attribution.py:727](../../scripts/audit_kt2_turnover_t2_attribution.py#L727)、[scripts/audit_kt2_turnover_t2_attribution.py:150](../../scripts/audit_kt2_turnover_t2_attribution.py#L150)。

### S17 · P1 · 研究插件只有冻结日对齐，缺少输入身份检查

插件有 input_sha256，但构建 HTML 只核代码、schema、experiments 类型和 asof。同日修订行情后旧研究仍可显示于新行情旁。

[run.py:502](../../run.py#L502)　` if plugin.get('asof')==payload['policy']['freeze']: `

**证据与现状**　源码路径；未据此判定当前插件所有数值过时。 **回归要求**　比对插件所声明的相关输入身份，显示过期状态或要求重建，再嵌入正式页。

关联位置　[run.py:499](../../run.py#L499)、[research/common.py:110](../../research/common.py#L110)、[README.md:89](../../README.md#L89)。

### S18 · P1 · 生成同源预期且路径集没有独立的最低覆盖

Python 本次回放结果同时充当 JS 逐行预期；空 paths 会跳过 JS 成交循环。若双端同错，parity 只能证明一致。合成现金不负、槽位上限等空路径也天然满足。

[tests/test_engine.py:130](../../tests/test_engine.py#L130)　` paths.append(dict(key=[mode,span,rate,delay,slip],r=r)) `

**证据与现状**　正常构造有枚举循环；已有阻单序列和税率手算值。JS 状态和前缀仍验证，不能把所有测试称为循环自证。 **回归要求**　固定预期路径键集和每日日期，加入独立按分微型账本；对某一笔或删掉整个场景应失败。

关联位置　[tests/test_engine.py:131](../../tests/test_engine.py#L131)、[tests/test_engine.py:140](../../tests/test_engine.py#L140)、[tests/test_engine.py:169](../../tests/test_engine.py#L169)、[tests/parity.cjs:34](../../tests/parity.cjs#L34)、[tests/parity.cjs:64](../../tests/parity.cjs#L64)、[tests/test_engine.py:94](../../tests/test_engine.py#L94)。

### S19 · P1 · 账本缺字段用空指标，缺 NAV 用初资贯穿

无成交文件/关键列返回 NaN，缺袖子默认趋势，未知方向可落入 SELL，缺 NAV 用 45 万常数。仍生成验收报告，很容易混淆碎单不可计算与真实占比。

[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:145](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L145)　` return empty `

**证据与现状**　S03 内存输入没有账本亦生成了排名。 **回归要求**　要求正式明细完整；可选诊断单列缺失原因、有效覆盖，拒绝未识别方向。

关联位置　[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:129](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L129)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:150](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L150)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:155](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L155)、[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:170](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L170)。

### S20 · P1 · Tushare 的零偏差标签覆盖了非对照门槛

无冻结成交额时只查供应商日均额达到 3 亿元。单日成交量允许差 100 股。生产者说明了此约定，采集器却称三股零偏差。

[research/verify_tushare_baseline.py:110](../../research/verify_tushare_baseline.py#L110)　` not original_amount and new_amount is not None and Decimal(new_amount)>=300_000_000) `

**证据与现状**　现存报告中 600900、002371 用 minimum_liquidity，600989 用 frozen_amount。 **回归要求**　分别报告价格校准、成交量容差、流动性门槛及证据覆盖；任何变更阈值须另行批准。

关联位置　[research/verify_tushare_baseline.py:119](../../research/verify_tushare_baseline.py#L119)、[research/verify_tushare_baseline.py:153](../../research/verify_tushare_baseline.py#L153)、[fetch_v4_data.py:521](../../fetch_v4_data.py#L521)、[results/research/tushare_baseline_check.md:20](../../results/research/tushare_baseline_check.md#L20)。

### S21 · P2 · confirmed 字段无法区分命令行确认与已有批准

policy.status 已批准时，即使没有传 --confirm-seeds，confirmed 也会写 true。普通命令在阻断下允许退出 0，文档已明示；审计消费方若只读进程状态仍易误判。

[run.py:563](../../run.py#L563)　` (OUTPUT/'data_audit.json').write_text(json.dumps(dict(approved=approved,confirmed=approved,coverage=d['coverage'], `

**证据与现状**　当前 confirmed=true 无法单独证明当时是否传了参数。 **回归要求**　将 approved_policy、explicit_confirm 及生产回放状态分开，保留诊断入口语义。

关联位置　[run.py:532](../../run.py#L532)、[run.py:586](../../run.py#L586)、[README.md:112](../../README.md#L112)。

### S22 · P2 · 空基金配置使分组校验天然为真

无基金配置和无基金记录时，分组检查过关且可没有 ETF 错误/覆盖行。股票引擎允许独立工作；只消费 etf_issues 的调用方会误把空分母视作完成。

[inputs.py:155](../../inputs.py#L155)　` require(all(r['group'] in ('蓝筹','成长') for r in funds.values()),'ETF 分组无效') `

**证据与现状**　当前 7 只 ETF 有实际覆盖。股票池另有 bool(uv) 守卫。 **回归要求**　ETF 独立输出配置数、行数、可用基金/日期与 disabled/partial 状态。

关联位置　[inputs.py:153](../../inputs.py#L153)、[offline/input.js:99](../../offline/input.js#L99)、[run.py:566](../../run.py#L566)、[offline/app.js:81](../../offline/app.js#L81)。

### S23 · P2 · 本地会话恢复异常被吞掉

索引库读取、结构转换或钱包校验失败后静默保留已渲染的初始状态，用户看不出为何未恢复；这与主题偏好读取失败无关。

[offline/app.js:488](../../offline/app.js#L488)　` }).catch(()=>{}); `

**证据与现状**　前端用户反馈缺口，不影响 Python 历史回放。 **回归要求**　显示恢复失败与当前数据来源，保留原存档。

关联位置　[offline/app.js:475](../../offline/app.js#L475)、[offline/app.js:486](../../offline/app.js#L486)、[offline/app.js:113](../../offline/app.js#L113)。

### S24 · P2 · 少量可解析观测即可通过单位校验

坏行被 continue，比较比例只以保留样本作分母；仅一条通过也可 unit_verified。采集退出另查非零覆盖，但二者都不能证明整段单位一致。

[research/factor_lab/fetch_factors.py:51](../../research/factor_lab/fetch_factors.py#L51)　` unit_verified=bool(n) and counts['single_side']/n >= .95) `

**证据与现状**　静态；空集有 bool(n) 守卫。 **回归要求**　报告应有/可比较/被剔除及原因；预先约定覆盖要求，新增稀疏反例。

关联位置　[research/factor_lab/fetch_factors.py:39](../../research/factor_lab/fetch_factors.py#L39)、[research/factor_lab/fetch_factors.py:162](../../research/factor_lab/fetch_factors.py#L162)、[research/factor_lab/fetch_factors.py:168](../../research/factor_lab/fetch_factors.py#L168)。

### S25 · P2 · 重新计算首批筛查时缺备份会改用当前池

首批底稿缺失时选用当前 universe；扩大后的正式池会改变首批比较基准，输出仍沿用首批命名。

[research/screen_50_candidates.py:409](../../research/screen_50_candidates.py#L409)　` seeds = read_table(BACKUP, 'universe') if BACKUP.exists() else raw['tables']['universe'] `

**证据与现状**　本地原 16 股备份存在。 **回归要求**　历史复算指定原池身份；缺备份时明确中止或作为另一次有标签的实验。

关联位置　[README.md:146](../../README.md#L146)、[research/exp_batch1_pool_comparison.py:85](../../research/exp_batch1_pool_comparison.py#L85)。

### S26 · P1 · 无效前瞻日历条目静默丢弃

无效日期或结构在 Python 被忽略，星期/来源不合格条目在双端被过滤。可能丢失下一交易日，使披露可用日无法验证，若无受影响 ETF 行就没有独立缺证状态。

[inputs.py:189](../../inputs.py#L189)　` continue `

**证据与现状**　不代表系统会自动提前使用 ETF 份额；特定披露校验另会拒绝。 **回归要求**　记录不合格行及缺失的可用时点；ETF 诊断与股票交易继续分开。

关联位置　[inputs.py:183](../../inputs.py#L183)、[inputs.py:190](../../inputs.py#L190)、[offline/input.js:104](../../offline/input.js#L104)、[inputs.py:358](../../inputs.py#L358)。

### S27 · P2 · Python 前缀从已处理全量对象裁剪，缺独立解析覆盖

Python prefix 沿用全量已导出的周界等派生状态，且 checks 无跨界窗口可返回 paths=0。JS 会重新 prepare(raw,cut)，缓解但未独立覆盖 Python 输入推导。

[run.py:37](../../run.py#L37)　` limited=cut_data(d,cut) `

**证据与现状**　现存记录实际检查 96 条前缀。 **回归要求**　两个实现均从相应原始输入重建；正式路径独立核验预期覆盖的窗口与场景。

关联位置　[run.py:27](../../run.py#L27)、[run.py:42](../../run.py#L42)、[run.py:51](../../run.py#L51)、[tests/parity.cjs:49](../../tests/parity.cjs#L49)。

### S28 · P2 · NAV 分母与交易日分母不足时有数值回退

指定了 NAV 表但窗口为空时，以初资为均值并把交易日数提至 1，仍可算出 242 倍年化。无 NAV 表时也用初资给出区间换手。

[scripts/audit_kt2_turnover_t2_attribution.py:205](../../scripts/audit_kt2_turnover_t2_attribution.py#L205)　` avg_nav = float(n["nav_rmb"].mean()) if len(n) > 0 else initial_capital `

**证据与现状**　影响归因报告的口径；账本自身仍由冻结引擎或研究执行器生成。 **回归要求**　缺 NAV 时将依赖均值的指标标为不可计算；分别显示市场日与成交日。

关联位置　[scripts/audit_kt2_turnover_t2_attribution.py:204](../../scripts/audit_kt2_turnover_t2_attribution.py#L204)、[scripts/audit_kt2_turnover_t2_attribution.py:210](../../scripts/audit_kt2_turnover_t2_attribution.py#L210)、[scripts/audit_kt2_turnover_t2_attribution.py:215](../../scripts/audit_kt2_turnover_t2_attribution.py#L215)。

### S29 · P1 · 只比较字段交集，却按模型数登记基线验证

任一边改名或删字段，交集缩小；旧指标字典空时循环完全跳过，baseline_checks 仍按窗数乘模型数上报。

[research/exp_batch1_pool_comparison.py:105](../../research/exp_batch1_pool_comparison.py#L105)　` for key in reference.keys() & metrics.keys(): `

**证据与现状**　静态，seed 身份和两池行情校验独立存在。 **回归要求**　定义最小必需指标及单位，验证它们逐一到达；回执记录实比字段与单元数。

关联位置　[research/exp_batch1_pool_comparison.py:106](../../research/exp_batch1_pool_comparison.py#L106)、[research/exp_batch1_pool_comparison.py:107](../../research/exp_batch1_pool_comparison.py#L107)。

### S30 · P1 · 先签发 JSON、后写明细，失败可留下跨批次结果

主页面先更新，随后三个正式结果文件逐个覆盖。如果在中途写入或渲染失败，目录可同时含新生产通过标志和旧明细。

[run.py:563](../../run.py#L563)　` (OUTPUT/'data_audit.json').write_text(json.dumps(dict(approved=approved,confirmed=approved,coverage=d['coverage'], `

**证据与现状**　异常中断条件；这次八张输入及两个证据文件 SHA 与现存回执一致。 **回归要求**　先完成一个独立运行目录，校验各输出后最后原子发布带 run_id 的指针。

关联位置　[run.py:547](../../run.py#L547)、[run.py:571](../../run.py#L571)、[run.py:580](../../run.py#L580)、[run.py:582](../../run.py#L582)。

### S31 · P2 · 锚点遍历已供应的案例，缺项会少核并正常返回

逐项指标有容差检查，但函数对缺失 pool/model/scenario 没有必需键集，返回已走过的计数，主入口只打印。正常 run_pool 明确构造场景。

[research/exp_50_pool_phase2b.py:209](../../research/exp_50_pool_phase2b.py#L209)　` for s,m in cases.items(): `

**证据与现状**　静态；与 S08 的准入聚合位于不同的检查边界。 **回归要求**　按约定矩阵生成期望单元，再逐项对账，数量与缺项写入回执。

关联位置　[research/exp_50_pool_phase2b.py:216](../../research/exp_50_pool_phase2b.py#L216)、[research/exp_50_pool_phase2b.py:421](../../research/exp_50_pool_phase2b.py#L421)。

### S32 · P1 · 连续净值切窗用窗内首个收盘作本金

第一天的收益在比值中被消掉，两套延迟账本首日不同会放大差值。原脚本不检查窗口分母与原账本起点一致。

[scripts/audit_kt2_turnover_t2_attribution.py:536](../../scripts/audit_kt2_turnover_t2_attribution.py#L536)　` ret_t1 = (n1["nav_rmb"].iloc[-1] / n1["nav_rmb"].iloc[0] - 1.0) * 100.0 `

**证据与现状**　既有复核将 W1 差值从 −52.03 pp 改为 −15.55 pp。 **回归要求**　明确区分独立起跑及连续切片，前者用初资，后者用窗前最后一个收盘。

关联位置　[scripts/audit_kt2_turnover_t2_attribution.py:537](../../scripts/audit_kt2_turnover_t2_attribution.py#L537)、[reports/phase2b_p1_attribution/attribution_crosscheck.md:22](../../reports/phase2b_p1_attribution/attribution_crosscheck.md#L22)、[reports/phase2b_p1_attribution/attribution_crosscheck.md:26](../../reports/phase2b_p1_attribution/attribution_crosscheck.md#L26)。

### 容易误判的命中

- 两个直接捕获 `Exception` 的 Python 处理器均保存错误，写失败回执，随后重新抛出。没有发现基线项目 Python 的裸 `except:`，也没有发现这两处捕获后直接返回 True/0。`research/exp_50_pool_phase2b_p2.py:579` 与 `scripts/diag_phase2b_p2_regimes.py:822` 的失败路径分别走到第 601、849 行。前端主题读写的空 catch 只影响偏好，会话恢复的异常另列 S23。
- `inputs.py:153` 在股票代码 all 之前先拒绝空股票池；`inputs.py:177` 要求历史索引非空。缺少 crosscheck 文件虽在 load 中读成 []，正式输入仍报错。P2 完整矩阵在 `research/exp_50_pool_phase2b_p2.py:532` 先核完整键集及 216 行，再做 all 检查。它的 PASS 常量出现在受检查的成功分支。
- 股票存量为零、无持仓、没有待派息项目时，账本对 dict.get 使用 0 有明确定义。行情供应商的动态方法查询与追溯证据不同，例如 `fetch_v4_data.py:207` 查找适配器方法；没有将全部 428 个取值候选一概定为缺陷。ETF 缺数、条件样本不足及初始化历史记录分开归类。

## 核心逻辑与单一事实源

以下数量按基线手写或生成后实际执行的定义计数，调用、UI 文本、测试预期和 HTML 内嵌副本单列。同一文件可能参与多种计算，各行不能相加为引擎总数。

### K01 · P1 · 5 个日级成交账本循环

按实际写现金、改仓、记成交的独立主体计数。冻结 Python/JS 各一份，另有机制、风险、哑铃三份。机制层增添月历与模型调度，风险层加入预算、整手及 resize，哑铃按袖子保存持仓/预留。TopK、因子与 P1/P2 调用或生成这些循环，没有再算作全新账本。

- [engine.py:202](../../engine.py#L202)　` cash+=gross-commission-stamp if sell else -gross-commission `
- [offline/engine.js:113](../../offline/engine.js#L113)　` cash+=sell?gross-commission-stamp:-gross-commission; `
- [research/exp_v5_mechanism_study.py:168](../../research/exp_v5_mechanism_study.py#L168)　` cash += gross - commission - stamp if sell else -gross - commission `
- [research/exp_v5_a2_risk_control.py:149](../../research/exp_v5_a2_risk_control.py#L149)　` cash += gross - commission - stamp if sell else -gross - commission `
- [research/exp_50_pool_barbell.py:217](../../research/exp_50_pool_barbell.py#L217)　` cash += qty*price-commission-stamp if sell else -qty*price-commission `

### K02 · P2 · 2 份正式费率和限价滑点，另有 2 个估算场景

正式两端均先按分取整、滑点后夹到价格界，最低佣金 500 分，卖税以 2023-08-28 切换；研究 Python 重用 fee/open_price。网页试算按收盘价固定 2bp，归因按成交名义额×7bp 估计并保留原税佣。后二者不模拟真实下一开盘、限价夹取及订单变化。

- [engine.py:101](../../engine.py#L101)　` commission=max(500,rounded(gross*25/100000)) `
- [engine.py:102](../../engine.py#L102)　` stamp=rounded(gross/(1000 if day<'2023-08-28' else 2000)) if sell else 0 `
- [engine.py:112](../../engine.py#L112)　` value=rounded(q['open']*100*(1+(-slip if sell else slip)/10000)) `
- [offline/engine.js:61](../../offline/engine.js#L61)　` return [Math.max(500,rounded(gross*25/100000)),sell?rounded(gross/(day<'2023-08-28'?1000:2000)):0]; `
- [offline/engine.js:64](../../offline/engine.js#L64)　` const v=rounded(q.open*100*(1+(sell?-slip:slip)/10000)); `
- [offline/app.js:225](../../offline/app.js#L225)　` const p=rounded(price*(1-2/10000)),[f,t]=fee(p,n,true,day); `
- [offline/app.js:232](../../offline/app.js#L232)　` const p=rounded(point[k].mark*100*(1+2/10000)),allocation=Math.min(budget,unit),n=canBuy(p,allocation,day); `
- [scripts/audit_kt2_turnover_t2_attribution.py:158](../../scripts/audit_kt2_turnover_t2_attribution.py#L158)　` df["sim_cost_7bp"] = df["commission"] + df["stamp_duty"] + df["notional"] * 0.0007 `

### K03 · P1 · 5 处成交账户结算，另有参考价校验及归因重建

五个账本均区分分红应收、到账与待上市送股，哑铃按证券和袖子登记，需维持整股取整次序。参考价链路另有采集 Decimal、Python 和 JS 两端验证，差异化折算与账户实际每股权益用途不同。归因/预算重建按持有发生日解释份额，不能拿来替代现金到账算法。

- [engine.py:239](../../engine.py#L239)　` share_due[key]=share_due.get(key,0)+floor(n*a['bonus']+1e-9) `
- [offline/engine.js:142](../../offline/engine.js#L142)　` shareDue[key]=(shareDue[key]??0)+Math.floor(n*a.bonus+1e-9); `
- [research/exp_v5_mechanism_study.py:196](../../research/exp_v5_mechanism_study.py#L196)　` share_due[action['share'], code] += floor(qty * action['bonus'] + 1e-9) `
- [research/exp_v5_a2_risk_control.py:177](../../research/exp_v5_a2_risk_control.py#L177)　` share_due[action['share'], code] += floor(qty * action['bonus'] + 1e-9) `
- [research/exp_50_pool_barbell.py:194](../../research/exp_50_pool_barbell.py#L194)　` shares_due[a['share'],s,code] += floor(qty*a['bonus']+1e-9) `
- [fetch_v4_data.py:339](../../fetch_v4_data.py#L339)　` return (previous-cash)/(1+bonus) `
- [inputs.py:277](../../inputs.py#L277)　` provisional=(previous-ref_div)/(1+ref_bonus) `
- [offline/input.js:176](../../offline/input.js#L176)　` const basis=terms??action,implied=(previous-basis.div)/(1+basis.bonus),error=Math.abs(implied-v.ref); `
- [scripts/diag_phase2b_p2_regimes.py:294](../../scripts/diag_phase2b_p2_regimes.py#L294)　` self.shares += floor(self.shares * action['bonus'] + 1e-9) `
- [phase2.py:42](../../phase2.py#L42)　` bonus=int(shares*action.get('bonus',0)+1e-9) `

### K04 · P1 · 周界 1 个生产器、2 个校验器；月度 4 处、3 种语义，双周 1 处

冻结等权在相邻交易日月份变化时处理，机制实验把传入序列最后一天也视为月末，哑铃从已公告周界取完整月的最后一个周信号，未完月不发。P1 另以 policy.start 所在周一锚定 14 天块，专用于双周趋势变体。相同的“月末”接口名不能承载三种日历。

- [fetch_v4_data.py:121](../../fetch_v4_data.py#L121)　` out.append(dict(date=d,week_end=str(end_week),source=CURRENT if d<=freeze else baourl('query_trade_dates'))) `
- [inputs.py:197](../../inputs.py#L197)　` expected=(d.isocalendar()[:2]!=date.fromisoformat(days[i+1]).isocalendar()[:2] `
- [offline/input.js:117](../../offline/input.js#L117)　` const exp=i+1<days.length?week(days[i+1])!==week(d):wd===5||r.week_end==='1'; `
- [engine.py:228](../../engine.py#L228)　` if mode=='Equal' and days[i-1][:7]!=day[:7]: `
- [offline/engine.js:133](../../offline/engine.js#L133)　` if(mode==='Equal'&&days[i-1].slice(0,7)!==day.slice(0,7)) `
- [research/exp_v5_mechanism_study.py:81](../../research/exp_v5_mechanism_study.py#L81)　` return {day for i, day in enumerate(days) if i == len(days) - 1 or day[:7] != days[i + 1][:7]} `
- [research/exp_50_pool_barbell.py:55](../../research/exp_50_pool_barbell.py#L55)　` if next_month <= horizon+timedelta(days=1): `
- [research/exp_50_pool_barbell.py:336](../../research/exp_50_pool_barbell.py#L336)　` month = month_closes(data['week_ends'],data['days'][-1],data['days'][-1]) `
- [scripts/run_phase2b_p1_ablation.py:61](../../scripts/run_phase2b_p1_ablation.py#L61)　` return {d for d, nxt in zip(days, next_days, strict=True) `

### K05 · P1 · 1 个生产波动序列，5 个权重上限实现点

AnnVol20 以 21 个可交易复权收盘形成 20 个对数收益，用样本标准差×√252。风险层取槽位与目标波动率之小者；TopK 叠加 50% 硬顶和 1/K，K2 归因显式写 0.5；哑铃乘趋势袖子份额；P2 再以状态后的名义单票上限封顶，波动预算仍从原份额出发。P2 字符串内用于匹配旧源码的公式只算一次运行实现。

- [research/exp_v5_a2_risk_control.py:70](../../research/exp_v5_a2_risk_control.py#L70)　` row[code] = stdev(b - a for a, b in zip(prices, prices[1:])) * sqrt(252) `
- [research/exp_v5_a2_risk_control.py:89](../../research/exp_v5_a2_risk_control.py#L89)　` weight = min(cap, rule.vol_target / annvol) if rule.vol_target is not None and annvol else cap `
- [research/exp_50_pool_topk_diag.py:158](../../research/exp_50_pool_topk_diag.py#L158)　` cap=min(.5,.25/volatility) if volatility else .5 `
- [research/k2_divergence.py:29](../../research/k2_divergence.py#L29)　` hard = min(.5, .25 / v) if v else .5 `
- [research/exp_50_pool_barbell.py:141](../../research/exp_50_pool_barbell.py#L141)　` weight = capitals[s]/100 * min(1/rule.kt, .25/v) `
- [research/exp_50_pool_phase2b_p2.py:77](../../research/exp_50_pool_phase2b_p2.py#L77)　` vol_weight = capitals[s]/100 * min(1/rule.kt, .25/v) `

### K06 · P1 · 5 个独立账户高水位计算点，另有事件拆分

计入两端账户最小回撤、A2 峰谷、诊断从 CSV 重建和 P2 收盘路径；不计只读取 dd 或校验风险决策净值的循环。冻结从实参初资起，A2 从固定 CAPITAL 分起算并用交叉乘法比较；CSV 诊断以元浮点重建。恢复日期 A2 要严格超过旧峰，诊断达到峰值即可，一天回到平价会产生不同恢复期。

- [engine.py:141](../../engine.py#L141)　` minimum=min(minimum,r['equity']/top-1) `
- [offline/engine.js:82](../../offline/engine.js#L82)　` for(const r of path){top=Math.max(top,r.equity);dd=Math.min(dd,r.equity/top-1);} `
- [research/exp_v5_a2_risk_control.py:377](../../research/exp_v5_a2_risk_control.py#L377)　` if equity * high < low * peak_equity: `
- [scripts/diag_phase2b_p2_regimes.py:177](../../scripts/diag_phase2b_p2_regimes.py#L177)　` row['drawdown_pct'] = 100 * (row['nav'] / peak - 1) `
- [research/exp_50_pool_phase2b_p2.py:146](../../research/exp_50_pool_phase2b_p2.py#L146)　` base_drawdown_pct=100 * (equity / peak - 1))) `
- [research/exp_v5_a2_risk_control.py:379](../../research/exp_v5_a2_risk_control.py#L379)　` recovery = next((r['date'] for r in path[trough + 1:] if r['equity'] > high), None) `
- [scripts/diag_phase2b_p2_regimes.py:440](../../scripts/diag_phase2b_p2_regimes.py#L440)　` if row['base_nav'] >= peak_value: `

### K07 · P1 · 2 个研究 Calmar 实现，2 个冻结 rr 实现

两份研究 value_metrics 同取首末自然日差/365.25 计算 CAGR，再除以最大回撤绝对值；DD 为零返回 None，单日窗口的年数为零未定义。rr 则以区间净收益为分子。TopK 已复用 prior；analytics.py 的个股诊断调用 replay，不能计作第三个 Python 账本。

- [research/exp_drop_300308_stress.py:49](../../research/exp_drop_300308_stress.py#L49)　` cagr = (1 + m['net']) ** (1 / years) - 1 `
- [research/exp_drop_300308_stress.py:50](../../research/exp_drop_300308_stress.py#L50)　` return {**m, 'cagr': cagr, 'calmar': cagr / -m['dd'] if m['dd'] else None, `
- [research/exp_v5_mechanism_study.py:333](../../research/exp_v5_mechanism_study.py#L333)　` cagr = (1 + m['net']) ** (1 / years) - 1 `
- [research/exp_v5_mechanism_study.py:335](../../research/exp_v5_mechanism_study.py#L335)　` return {**m, 'cagr': cagr, 'calmar': cagr / -m['dd'] if m['dd'] else None, `
- [engine.py:143](../../engine.py#L143)　` return dict(net=net,dd=minimum,rr=net/-minimum if minimum else None, `
- [offline/engine.js:84](../../offline/engine.js#L84)　` return {net,dd,rr:dd?net/-dd:null,weight:mean(path.map(r=>r.weight)),flat:mean(path.map(r=>!Object.keys(r.hold).length)), `
- [research/exp_50_pool_topk_diag.py:232](../../research/exp_50_pool_topk_diag.py#L232)　` m=prior.value_metrics(book) `
- [analytics.py:52](../../analytics.py#L52)　` m=replay(d,s,'T1',w,code=code)['metrics'], `

### K08 · P1 · 4 处账户换手计算，3 种输出语义

哑铃按自然年分组求该年窗口内单边比率；P1 导出与滚动复核均算 50×总成交分额×天数/每日净值分额之和，即整个窗口单边百分比；归因另算双边区间和按 242 个交易日折年。ETF radar 的 turnover 是基金成交额，不计为账户换手。

- [research/exp_50_pool_barbell.py:361](../../research/exp_50_pool_barbell.py#L361)　` stats['yearly'] = {y:dict(turnover=gross[y]/(2*mean(v)),trades=trades[y]) for y,v in years.items()} `
- [scripts/run_phase2b_p1_ablation.py:188](../../scripts/run_phase2b_p1_ablation.py#L188)　` turnover = float(Fraction(50 * gross * len(ledger['path']), equity_sum)) `
- [reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py:50](../../reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py#L50)　` turnover=Fraction(50*gross*len(path),sum(navs.values())), `
- [scripts/audit_kt2_turnover_t2_attribution.py:214](../../scripts/audit_kt2_turnover_t2_attribution.py#L214)　` period_turnover_pct = total_notional / avg_nav * 100.0 `
- [scripts/audit_kt2_turnover_t2_attribution.py:215](../../scripts/audit_kt2_turnover_t2_attribution.py#L215)　` ann_turnover_pct = period_turnover_pct * (242.0 / n_days) if n is not None else np.nan `
- [analytics.py:118](../../analytics.py#L118)　` turnover=[sum(funds[k][x][3] for k in blue) for x in span] if blue else [] `

## 工程规模、耦合与无效接口

### E01 · P1 · P2 前后清单重复读取整个缓存和历史运行时

诊断清单 22,332 项，消融清单 22,341 项，各自 before/after 相同，各含 21,550 个缓存路径。仅缓存按当前字节计，每次前后两遍逻辑读取 2,160,141,634 B，未计其他输入或 OS 缓存；没有执行基准计时。两份 JSON 合计 16,246,362 B。

- [scripts/diag_phase2b_p2_regimes.py:19](../../scripts/diag_phase2b_p2_regimes.py#L19)　` PROTECTED_DIRS = ('data', 'research', 'results', 'reports/phase2b_p1_ablation') `
- [scripts/diag_phase2b_p2_regimes.py:59](../../scripts/diag_phase2b_p2_regimes.py#L59)　` paths.update(p for p in (ROOT / folder).rglob('*') if p.is_file()) `
- [scripts/diag_phase2b_p2_regimes.py:825](../../scripts/diag_phase2b_p2_regimes.py#L825)　` before, after = manifest['sha256_before'], snapshot() `

关联位置　[research/exp_50_pool_phase2b_p2.py:43](../../research/exp_50_pool_phase2b_p2.py#L43)、[research/exp_50_pool_phase2b_p2.py:581](../../research/exp_50_pool_phase2b_p2.py#L581)。

### E02 · P1 · 研究验证遍历整仓并扫描本地环境

有令牌时逐文件打开，范围还包含 .venv 和 fetch_cache，只排 .git 与根凭据文件。此处目的是泄漏核验，与数据身份哈希不同。可在发布/传输边界核验待发布内容；完整本地扫描仍可作为单独取证命令。本审计未读取令牌或执行该命令。

- [research/exp_50_pool_phase2b.py:279](../../research/exp_50_pool_phase2b.py#L279)　` for folder,dirs,names in os.walk(ROOT): `
- [research/exp_50_pool_phase2b.py:280](../../research/exp_50_pool_phase2b.py#L280)　` dirs[:]=[d for d in dirs if d!='.git'] `
- [research/exp_50_pool_phase2b.py:289](../../research/exp_50_pool_phase2b.py#L289)　` with mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as content: `

关联位置　[research/exp_50_pool_phase2b.py:425](../../research/exp_50_pool_phase2b.py#L425)。

### E03 · P1 · 用函数源码替换构造回测器

P1 锁定整个哑铃脚本的字节并替换函数文本，P2 再替换 inspect 输入。即使仅改目标文件中的无关注释，也可能触发哈希失败；源码内空格与局部变量名成为接口。保留当前复现入口，在新的研究账本里显式传入目标与风控函数。

- [scripts/run_phase2b_p1_ablation.py:81](../../scripts/run_phase2b_p1_ablation.py#L81)　` if digest(source_path) != BASE_BOOK_SHA: `
- [scripts/run_phase2b_p1_ablation.py:135](../../scripts/run_phase2b_p1_ablation.py#L135)　` if source.count(old) != 1: `
- [scripts/run_phase2b_p1_ablation.py:139](../../scripts/run_phase2b_p1_ablation.py#L139)　` exec(compile(source, f'{source_path} [read-only ablation hooks]', 'exec'), scope) `

关联位置　[research/exp_50_pool_phase2b_p2.py:101](../../research/exp_50_pool_phase2b_p2.py#L101)、[research/exp_50_pool_phase2b_p2.py:104](../../research/exp_50_pool_phase2b_p2.py#L104)。

### E04 · P2 · 两个执行脚本藏在报告树中

按 reports/ 一刀切会漏扫程序或删掉复算入口。固定报告生产者和相对路径也参与历史身份；迁移应先调整发现/复现入口并在归档包留原路径。

- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:297](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L297)　` def main() -> None: `
- [reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py:170](../../reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py#L170)　` def main(): `
- [scripts/diag_phase2b_p2_regimes.py:19](../../scripts/diag_phase2b_p2_regimes.py#L19)　` PROTECTED_DIRS = ('data', 'research', 'results', 'reports/phase2b_p1_ablation') `

### E05 · P2 · 重复账本有精确字节证据

reports 下按 SHA 分组，96 组相同 CSV，多余 136 份、10,923,948 B。部分协议显式计数 216 个 nav 和 216 个 trades；直接删文件会破坏复现。用路径→内容散列的索引或能物化原路径的归档，保留每个 run 的语义。

- [scripts/run_phase2b_p1_ablation.py:182](../../scripts/run_phase2b_p1_ablation.py#L182)　` def export_book(ledger, summary, variant, pool, window, scenario): `
- [research/exp_50_pool_phase2b_p2.py:545](../../research/exp_50_pool_phase2b_p2.py#L545)　` tally = sum(2 for _ in OUT.glob('runs/*/*/*/*/nav.csv')) `
- [reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py:37](../../reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py#L37)　` def market_metrics(ledger): `

### E06 · P2 · 采集与发布、正式回放与展示分别在一个入口串行

fetch_v4_data.py 1,130 行，main 同时选择来源、暂存、审计并发布；run.py 590 行，正式回放与 HTML/Markdown 构建强耦合。先提取无副作用的身份检查和输出发布；不为每个供应商再增加一层类。

- [fetch_v4_data.py:812](../../fetch_v4_data.py#L812)　` def main(): `
- [fetch_v4_data.py:1089](../../fetch_v4_data.py#L1089)　` audited=prepare({**raw,'tables':audit_tables,'calendar_lookahead':lookahead},finish) `
- [fetch_v4_data.py:1117](../../fetch_v4_data.py#L1117)　` (DATA/'fetch_provenance.json').write_text(json.dumps(dict(target=finish,dates=len(days),archive=archives, `

关联位置　[run.py:491](../../run.py#L491)、[run.py:550](../../run.py#L550)、[run.py:582](../../run.py#L582)。

### E07 · P2 · 发现一个无效入参，未证明存在可删的业务类

market_index 的 workspace 形参在函数 AST 中零读取；当前内部调用仍传它。可在保持调用兼容的前提下收窄。statistics 的 _ 和 plan 的回调参数属于调用约定。HTMLParser 钩子、unittest 类和命令行 main 由框架/入口发现，仅凭零显式引用不能删除。

- [fetch_v4_data.py:621](../../fetch_v4_data.py#L621)　` def market_index(provider,root,workspace,start,end,warn): `
- [fetch_v4_data.py:1059](../../fetch_v4_data.py#L1059)　` previous=protected_index.get((row['date'],)) `
- [research/exp_600428_earnings.py:82](../../research/exp_600428_earnings.py#L82)　` def statistics(trades, _): `

关联位置　[research/factor_lab/court.py:110](../../research/factor_lab/court.py#L110)。

### E08 · P2 · 历史实验模块成为当前代码的基础库

旧研究 main 绑定专属池与对照，但同一文件的常量、指标、回放函数仍被新研究导入。从文件名断定全部 exp_* 过期会断掉 Barbell→TopK→A2→mechanism 链。

- [research/exp_50_pool_barbell.py:12](../../research/exp_50_pool_barbell.py#L12)　` import exp_50_pool_topk_diag as top `
- [research/exp_50_pool_topk_diag.py:12](../../research/exp_50_pool_topk_diag.py#L12)　` import exp_v5_a2_risk_control as risk `
- [research/exp_v5_a2_risk_control.py:12](../../research/exp_v5_a2_risk_control.py#L12)　` import exp_v5_mechanism_study as prior `

关联位置　[research/exp_v5_mechanism_study.py:642](../../research/exp_v5_mechanism_study.py#L642)、[research/factor_lab/run_phase2a.py:11](../../research/factor_lab/run_phase2a.py#L11)。

## 文档、注释与实际行为

### D01 · P2 · 导入池的未来时与现行 50 股混用

AGENTS 将 16→50 称为后续计划，README 扩容段保留分批过程；ROADMAP 的导入行链接了可变的当前名单。把导入快照指向原 16 股底稿，现状指向带输入哈希的 50 股审计。README 已注明上表是历史，勿改历史数字。

- [AGENTS.md:8](../../AGENTS.md#L8)　`` - v4.0 导入基线为 16 只种子股、7 只 ETF、初始研究资金 ¥450,000，重点研究 Top 1 / Top 2。标的配置读取 `universe.csv`、`funds.csv`；实际配置与进度记录在 [README](README.md)、[初始化记录](docs/INITIALIZATION.md) 和 [路线图](docs/ROADMAP.md)。16→50 为后续研究计划。 ``
- [README.md:15](../../README.md#L15)　` | 股票池 / ETF | 16 只种子股，当前 7 个股票桶；7 只宽基 ETF | `
- [README.md:25](../../README.md#L25)　`` 上表记录导入时的状态。`results/` 记录最近一次本地验收。原包结果可以从仓库历史中查看，本地采集前的底稿和结果也保存在 `data/fetch_cache/baseline-2026-11-23/`。新克隆时可用的行情取决于本地缓存和数据服务，验收须核对同一次运行的输入哈希。 ``

关联位置　[README.md:144](../../README.md#L144)、[docs/ROADMAP.md:11](../../docs/ROADMAP.md#L11)、[results/rotation_v4_0_validation.md:7](../../results/rotation_v4_0_validation.md#L7)。

### D02 · P1 · 工程完整性还锁定三个输入协议文件

两份冻结引擎清单正确；工程命令另验 policy.json、inputs.py、offline/input.js，正式 run 没有执行这三项批准比较。新工程师只读文档会低估修改验证器的影响。分别记录策略冻结、协议锁和正式入口实际执行范围。

- [AGENTS.md:22](../../AGENTS.md#L22)　`` 机器可读锁为 `engine_integrity.json`。除非用户明确批准解除冻结并进入新的策略版本，否则不得更改这两个文件的任何字节，也不得修改锁值来迁就差异。`.gitattributes` 保护冻结文件免受 Windows 换行转换影响。发现差异先核实来源、换行和版本，不自动修锁。 ``
- [README.md:129](../../README.md#L129)　` | engine_integrity.json、scripts/check_integrity.py | 原锁文件与固定的批准哈希核验；不允许静默更换基线 | `
- [scripts/check_integrity.py:12](../../scripts/check_integrity.py#L12)　` APPROVED_INPUT_PROTOCOL = { `

关联位置　[scripts/check_integrity.py:24](../../scripts/check_integrity.py#L24)、[run.py:538](../../run.py#L538)。

### D03 · P1 · ann_oneway_turnover_pct 没有年化

导出字段名与验收脚本顶部写成单边年化，实际值是整个窗口的单边百分比。P1 manifest 已说明无年化乘子；P2 映射到 oneway_turnover_pct。保留老字段读兼容，在新协议写清期间、单双边、单位。

- [scripts/run_phase2b_p1_ablation.py:216](../../scripts/run_phase2b_p1_ablation.py#L216)　` ann_oneway_turnover_pct=turnover, `
- [scripts/run_phase2b_p1_ablation.py:518](../../scripts/run_phase2b_p1_ablation.py#L518)　` turnover='Window gross fills / (2 * average daily equity), percent, no annualization multiplier', `
- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:8](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L8)　` 3. 汇总全部消融变体在 50/49 股 × 6 窗 × (2bp / 7bp / T+2) 下的收益、回撤、卡玛、单边年化换手、 `

关联位置　[reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:26](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L26)、[research/exp_50_pool_phase2b_p2.py:219](../../research/exp_50_pool_phase2b_p2.py#L219)。

### D04 · P1 · 零误差文字混用了精确账本与容差摘要

evaluator 只用 11 个锚点，收益与回撤容许 0.05 个百分点，成交笔数取 int 后比较；11 个键没有覆盖全部 36 格。原生导出器另做逐项比较，P2 另对 36 组 BASE 账本散列；这些证据应分别命名。

- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:7](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L7)　` 2. 校验 BASE 变体是否与阶段 2A-Plus 冻结基线（50/49 股 × W1/IS/OOS/W_Bear/W5/2026）零误差对齐。 `
- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:220](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L220)　` def verify_baseline_anchors(df: pd.DataFrame, tol_pct: float = 0.05) -> pd.DataFrame: `
- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:236](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L236)　` abs(float(r["net_return_pct"]) - exp_net) <= tol_pct `

关联位置　[scripts/run_phase2b_p1_ablation.py:384](../../scripts/run_phase2b_p1_ablation.py#L384)、[research/exp_50_pool_phase2b_p2.py:285](../../research/exp_50_pool_phase2b_p2.py#L285)。

### D05 · P1 · 防御槽数默认 5，生成原账本使用 3

50/50 Kt2 归因以默认槽数估算单槽预算及整手闲置，原账本的防御槽数是三。归因 inputs/input_manifest.json 的 sleeve_slots 也记录 3。CLI 可显式覆盖；不能由默认值反推出历史命令。

- [scripts/audit_kt2_turnover_t2_attribution.py:715](../../scripts/audit_kt2_turnover_t2_attribution.py#L715)　` p.add_argument("--defense-slots", type=int, default=5, help="防御袖子目标持仓数，默认 5") `
- [scripts/audit_kt2_turnover_t2_attribution.py:289](../../scripts/audit_kt2_turnover_t2_attribution.py#L289)　` def est_slot_budget(slv: str) -> float: `
- [research/exp_50_pool_barbell.py:108](../../research/exp_50_pool_barbell.py#L108)　` kv = 3 `

关联位置　[scripts/audit_kt2_turnover_t2_attribution.py:292](../../scripts/audit_kt2_turnover_t2_attribution.py#L292)。

### D06 · P1 · 相同窗口标签对应不同边界

归因默认 W_Bear 取 2021-02-18 至 2024-02-05，冻结窗是 2021-03-01 至 2023-11-19。W1、OOS 还存在自然日界与首个交易日的标签差异，后两者有时选中相同会话。按日历核对后再比较同窗绩效。

- [scripts/audit_kt2_turnover_t2_attribution.py:29](../../scripts/audit_kt2_turnover_t2_attribution.py#L29)　` "W_Bear": ("2021-02-18", "2024-02-05"),  # 请按仓库冻结 W_Bear 窗口起止日核对覆盖 `
- [policy.json:11](../../policy.json#L11)　` "W_Bear": ["2021-03-01", "2023-11-19"], `
- [scripts/diag_phase2b_p2_regimes.py:27](../../scripts/diag_phase2b_p2_regimes.py#L27)　` 'OOS': ('2023-01-21', '2026-11-20'), 'W_Bear': ('2021-03-01', '2023-11-19'), `

关联位置　[reports/phase2b_p1_attribution/attribution_crosscheck.md:27](../../reports/phase2b_p1_attribution/attribution_crosscheck.md#L27)。

### D07 · P2 · 半月轮动是已有澄清过的文字冲突

BASE 防御只在完整月份最后的周信号选股；不是每月最后一个交易日，也不是每两周。双周控制作用于趋势变体。现存 P2 报告已明确区分最初措辞与实际实现；未发现需要将防御改成双周的依据。

- [reports/phase2b_p2_ablation/p2_ablation_report.md:7](../../reports/phase2b_p2_ablation/p2_ablation_report.md#L7)　` 每窗起点 ¥450,000。P1 纯 BASE 的防御袖子使用月末最后一个周信号及 50% 预算。本次文字中的半月轮动与归档实现不同，实验采用归档日历以保持 BASE 账本精确复现。趋势 gate、排名、Top-2、keep_rank=4、日度硬离场和波动上限沿用 P1。HALF 将总上限降为 25%，单票取 12.5% 和原波动上限的较小值。ZERO 在应执行日停止新增仓位，已持有仓位按原撮合约束卖出。趋势释放预算不转给防御袖子。恢复正常后沿用 BASE 的建仓与留仓规则。 `
- [research/exp_50_pool_barbell.py:149](../../research/exp_50_pool_barbell.py#L149)　` if day in months: `
- [research/exp_50_pool_barbell.py:336](../../research/exp_50_pool_barbell.py#L336)　` month = month_closes(data['week_ends'],data['days'][-1],data['days'][-1]) `

关联位置　[scripts/run_phase2b_p1_ablation.py:91](../../scripts/run_phase2b_p1_ablation.py#L91)、[research/exp_50_pool_phase2b_p2.py:569](../../research/exp_50_pool_phase2b_p2.py#L569)。

### D08 · P2 · P1 evaluator 的脚本位置与 manifest 默认值都已漂移

头部称 scripts/ 路径并提供默认 manifest，实际脚本在 reports/，参数 required=True。无参执行不会读取说明给出的文件。

- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:3](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L3)　` Phase 2B-P1 消融验收与只读审计汇总脚本 (scripts/eval_phase2b_p1_ablation.py) `
- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:12](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L12)　` 标准输入约定 (--manifest，默认 reports/phase2b_p1_ablation/runs/ablation_manifest.json)： `
- [reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:300](../../reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py#L300)　` p.add_argument("--manifest", type=Path, required=True, help="消融运行产物 ablation_manifest.json 路径") `

### D09 · P2 · 扩容与因子研究测试未进入现有 CI

当前工作流跑 test_fetch 和 test_engine，后者串 Node 合成/前缀/UI；Phase 2B 自己另跑 test_phase2b 和 test_factor_lab。初始化记录的 CI 通过对应当时提交，不能外推至当前未提交的 P1/P2/evaluator。

- [.github/workflows/ci.yml:55](../../.github/workflows/ci.yml#L55)　` - name: Test collector normalization without network `
- [.github/workflows/ci.yml:61](../../.github/workflows/ci.yml#L61)　` - name: Test Python engine, JS parity, prefix causality and offline UI `
- [research/exp_50_pool_phase2b.py:41](../../research/exp_50_pool_phase2b.py#L41)　` VERIFICATION = ( `

关联位置　[docs/INITIALIZATION.md:31](../../docs/INITIALIZATION.md#L31)。

### D10 · P2 · 无参数 parity 命令不只跑 Node 单项

扩容模板把无参命令列为独立排查入口；它实际再启动 Python 合成测试并重写 synthetic_verification.json。AGENTS 对重复运行已有说明。指定输入包与无参复算应分别标注。

- [docs/ai-prompts/bucket-expansion.md:65](../../docs/ai-prompts/bucket-expansion.md#L65)　`` 上述采集和正式回放应分别检查实际退出状态；采集失败时保留原始数据与缺口，不能以随后生成页面冒充正式通过。`tests/test_engine.py` 已调用 Node parity，正式回放通过数据验收后也会调用真实数据对账；独立排查双端问题时可运行 `node tests/parity.cjs`，无需为每次文档编辑重复整套计算。 ``
- [tests/parity.cjs:2](../../tests/parity.cjs#L2)　` if(!process.argv[2]){ `
- [tests/parity.cjs:3](../../tests/parity.cjs#L3)　` require('node:child_process').execFileSync('python',[path.join(__dirname,'test_engine.py'),'--parity-only'],{stdio:'inherit'}); `

关联位置　[tests/test_engine.py:177](../../tests/test_engine.py#L177)。

### D11 · P2 · 离线页面已成为部分研究身份的一部分

HTML 对工程是可再生成的大产物，但部分实验哈希它并与历史报告的 identity 比较，缺页或仅改 UI 也可能阻断复算。归档包应保存受锁定的原页或其可复现资源。

- [README.md:59](../../README.md#L59)　`` `prepare_workspace.py` 仅为不存在的 `data/stocks.csv`、`data/etf.csv` 建立正确表头，并从当前源码构建 `rotation_quant_lab_v4_0_offline.html`。已有数据不覆盖，正式审计和报告不改写。新克隆必须先执行这一步，因为原采集入口也会先读取这两张 CSV。 ``
- [README.md:138](../../README.md#L138)　`` `.gitignore` 排除可重拉的 `data/stocks.csv`、`data/etf.csv`、`data/fetch_cache/`、本地采集日志/回执以及大体积构建产物；人工核验补丁和历史研究证据保留。发布新的研究结果时，应保留其数据身份和必要采集回执，不能只提交一个缺乏来源的收益表。 ``
- [research/exp_50_pool_topk_diag.py:23](../../research/exp_50_pool_topk_diag.py#L23)　` SNAPSHOT = (*FROZEN, 'universe.csv','results/data_audit.json','results/rotation_v4_0_validation.md', `

关联位置　[research/exp_50_pool_topk_diag.py:43](../../research/exp_50_pool_topk_diag.py#L43)、[research/exp_50_pool_barbell.py:322](../../research/exp_50_pool_barbell.py#L322)。

### D12 · P1 · 校准报错称零偏差，实际写明多种容差与替代门槛

与 S20 同根的文档问题，不重复计入 P0。采集器提示应引用各维度结论，避免把流动性证据转述为成交额一致。

- [fetch_v4_data.py:521](../../fetch_v4_data.py#L521)　` blockers.append('Tushare 三股零偏差门禁未通过，详见 results/research/tushare_baseline_check.md') `
- [research/verify_tushare_baseline.py:109](../../research/verify_tushare_baseline.py#L109)　` amount_passed = (relative_error <= Decimal('1e-6') if relative_error is not None else `
- [results/research/tushare_baseline_check.md:16](../../results/research/tushare_baseline_check.md#L16)　` 除权日参考价差须在 ¥0.005 以内且符合分位舍入。冻结成交额相对误差上限为 1e-6，缺整列的仅检查日均额达到 3 亿元。单日成交量允许相差最多 100 股。 `

## 体积、依赖与归档边界

| 基线范围 | 文件数 | 逻辑字节 | 处理依据 |
|---|---:|---:|---|
| data/fetch_cache/ | 21,550 | 1,080,070,817 | 内含 runtimes/ 的 17,693 项、604,945,054 B。这里还有旧快照、数据回执和可运行的 3.11；从例行哈希范围剔除与删除文件是两件事 |
| reports/ | 1,173 | 122,270,262 | 包含 2 个 Python 程序。其余主要是成组的 NAV、逐笔及报告；相同摘要名不能作为重复判据 |
| results/ | 35 | 17,363,061 | 有生成的历史结果，也有被后续研究读取的对照依据 |
| 离线单页 | 1 | 15,134,038 | 同时内嵌行情字典和源码。可重建，但旧研究有页面哈希依赖 |
| results/rotation_research.json | 1 | 6,852,088 | 已计入 results/，正式产物也是研究基线的输入，不能二次计入清理空间 |
| 本地 Python 字节码（全项目，缓存/环境以外） | 32 | 1,118,896 | 关闭正在使用它们的进程后可再生成；不进入历史证据清理清单 |
| data/fetch_warnings.log | 1 | 2 | 当前体积极小，不构成优化目标 |

既有 reports/ 与 results/ 的非代码研究产物为 **1,155 CSV、27 JSON、24 Markdown**。按 SHA-256 对比 reports/ 的 CSV、JSON、HTML、Markdown 与日志，96 个重复组全为 CSV。示例中，`reports/phase2b_p1_ablation/rolling_retest/runs/49/2026/2bp/BASE/nav.csv` 与 `reports/phase2b_p1_ablation/runs/49/2026/2bp/BASE/nav.csv` 的 27,159 字节完全相同。只把同组首份之外的字节计为可节约量。清单中可按 sha256 查出每个重复组。两份 P2 manifest 虽各自重复写 before/after，但两个 manifest 的内容有差别，未把它们算作可直接删的重复文件。

### 可归档、可删和仍被调用的对象

| 对象 | 判断 | 留存或迁移条件 |
|---|---|---|
| `research/exp_drop_300308_stress.py:20` · `POOL_NAMES = {'full': '16 股', ...}` | P2，16/15/14 股历史压测入口；项目内未发现其他 Python 模块导入本脚本 | 可随当时股票池、输入身份、JSON/MD 与启动方式一起归档。保留逐笔归因解释 |
| `research/exp_batch1_pool_comparison.py:95` · `seed_set = ... read_table(BACKUP, 'universe')` | P2，首批比较的叶子 CLI；当前全池已经变化 | 归档时保留 16/35 股两个历史名单及结果。直接用当前池复算会比较不同的对象 |
| `scripts/audit_kt2_turnover_t2_attribution.py:786` · `report_path = args.out_dir / "audit_kt2_turnover_t2_report.md"` | P2，独立诊断 CLI；归因报告与复核记录一起有历史价值 | 可归入历史研究工具并保留原命令兼容入口，旧报告与更正后的交叉复核要成对保留 |
| `reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py:297` · `def main() -> None:` | P2，报告生成器，无显式 Python 导入者 | 最先修退出契约；迁出报告树时保留旧 CLI、锚点 CSV 和摘要。P2 会读取 P1 锚点 |
| `research/exp_v5_mechanism_study.py:329` · `def value_metrics(book):`、`research/exp_v5_a2_risk_control.py:106` · `def replay_risk(...)` | P1，历史 main 与活跃库函数共处一文件 | 先分离被调用函数再归档 main；Barbell、TopK、因子实验及复核脚本都依赖这条链 |
| `fetch_v4_data.py:621` · `def market_index(provider,root,workspace,start,end,warn):` | P2，无效的 workspace 参数 | 单独移除内部参数并更新调用，或保留兼容签名；不画成需要注入的服务 |
| `docs/archive/`、人工核验底稿、输入快照、合成及真实基线 | 来源证据或对照 | 维持身份索引与可读副本。可删除候选限于可重建字节码、已验证且可恢复原路径的额外内容副本 |

归档候选的依据是入口、导入关系和读写的具体产物。操作系统任务、用户手工调用及仓外消费者未在本次静态范围中，无法证实任何整个业务脚本可直接永久删除。核心 Python 项目内的零引用扫描还命中 HTMLParser 回调、unittest 测试类及 urllib 重定向钩子；保留框架调用关系。

## 目标架构与迁移顺序

采用一个研究侧 Python 账本、少量纯函数和既有离线 JS 镜像。保留根目录薄入口，先允许旧版本按原路径复现。下列路径是蓝图，本次没有创建这些目录。

```text
engine.py + offline/engine.js   v4 冻结工件，留在现有相对路径
core/                          导入冻结交易原语；研究共享的指标、日历与权益结算
data_pipeline/                 原始采集和回执 → 分批落地 → 契约校验
strategies/                    选股、袖子、排序缓冲和目标预算的纯函数
backtest/                      周期调度、开盘可成交检查、账户流水与验收发布
research/                      可运行实验，显式声明输入、场景及比较窗口
offline/                       输入与指标镜像、页面构建和展示
tests/                         微型独立账本、门禁负例、双端/前缀检查
archive/                       历史入口、输入身份、产物及复现命令
```

初期 `core/` 的交易原语直接调用现有冻结 Python 实现。JS 保留独立实现与跨端测试。策略函数接收当时可见的数据，只返回目标排序、预算、调仓日期和原因；现金、税佣、取整、权益到账以及延迟成交统一在研究侧账本执行。制表层消费明确标注单位和分母的结果。无需基类层次、插件注册中心、数据库迁移或新服务。

| 顺序 | 最小变更 | 验收与退出条件 |
|---|---|---|
| 1. 先修验收边界 | 统一正式入口的批准哈希键集、批次身份、必需证据和退出语义。缺失、错误、未运行与通过四种状态单列；ETF 覆盖仍独立 | 空锁/漏锁、缺回执键、旧校准、漂移锚点、少一池、少一场景、优化解释器分别有预期失败。保留诊断模式；成功样本仍能发布 |
| 2. 留下可人工核算的账本 | 测试夹具放在 tests。将下表小账本作为静态预期，禁止测试时用被测实现生成或刷新预期 | 按交易日逐笔核对证券、方向、信号日、成交价、手数、税佣、现金、权益、待到账现金/股及可卖股数。金额以分做整数精确比较；特征浮点沿用已有容差 |
| 3. 先拆指标、日历，再拆研究账本 | 给 period_oneway、annualized_twoway、net_over_dd 和 calmar 独立字段与单位。给日历明确区分交易月末、最后完整周、固定双周块 | 用短窗与整年、平峰恢复和节前短周反例判别。影子运行新旧研究实现，每次只迁移单个研究调用者 |
| 4. 去掉源码替换 | 在研究副本上接入目标预算、持仓调整和审计回调；先迁移机制和风险规则，再迁移哑铃 BASE、P1、P2。保留 v4 两个冻结文件及路径 | 选定 BASE/双袖子/延迟的存档路径按分比对；完整 36 组 BASE 作为迁移里程碑，避免在每次文案改动上跑满 216 个格子。新策略版本须另获批准 |
| 5. 缩去 I/O 并安全归档 | 对八张 CSV、实际使用的公告/核验底稿、因子表、日历、运行器、锁与场景配置建立明确的输入清单；来源采集时算内容哈希并绑定只读批次。缓存镜像和解释器按独立包身份保存 | 缺必需键、摘要不符或输入批次变化时拒绝。不能把 mtime/大小当作内容证据。有变化、无可靠不可变依据或正式发布时，重新计算**实际输入**的散列；需要排查原始来源时再做全缓存取证 |
| 6. 产物发布与兼容 | run_id 绑定输入/引擎/协议/代码/场景。先在新批次目录完成所有输出和校验，成功后一次切换索引。重复账本按内容寻址，保留可还原原路径的轻量清单 | 与原始文件定长、逐字节或同 SHA 还原一致；中途中断不改变上一批已发布结果。可以撤回到旧入口及其完整证据包 |

### 最小 Golden Master

1. **下一可成交开盘。** 复用 `tests/test_engine.py:79` 的阻单序列，把信号日、首个合格开盘、价格边界、成交和余额写成手算常量。跨 2023-08-28 的等额卖单可直接借用该测试已有的税额期望，覆盖最低 500 分佣金，并保留零买单/不足一手的独立日期。改变一笔佣金 1 分必须失败。
2. **权益到账。** 建一个明确前收、现金分派和送股的双标的账本，让除权、派息、股份可卖分落不同交易日，穿过一次停牌。预期分别列可用现金、应收、可卖与待上市股数；参考价使用已批准容差，账本金额零误差。错误的公告日或折算底稿在输入层即报错。
3. **时间与双袖子。** 在有真实短周、月末最后周信号与最后交易日不同的微型日历上，写定防御/趋势目标与 2bp、7bp、T+2 三种情景的成交日。测试周中截断、月尚未结束与相同信号跨袖子互斥；分别从截断原始数据重建 Python/JS。
4. **一个封存的真实 BASE 切片。** 从已有 36 组精确对账产物选择可覆盖一次权益或延迟成交的连续片段，将开端完整状态和原账本摘要一并封存；片段须保留预热、应收与待执行指令，不能在中间凭空重新投入 45 万。事先记录路径、输入和预期哈希。涉及核算重大迁移，再验证存档全窗。

### 文档取证与数据状态

- 已分别核对统一规范、README、Claude/Cursor 引用、初始化记录、路线图、扩容模板、归档 README、研究设计和本地生成的 Markdown。历史初始化的 16 股、1 条 ETF 和旧 CI 对应其记录日期，留作历史。报告类文档追溯到各自的运行器或已标识的复核记录，保留原报告及其纠正说明。对 Markdown/MDC 提取出的 43 处有路径的本地行内链接，目标均存在；没有联网检查远端链接。
- 本地既有 `results/data_audit.json`、`results/rotation_research.json` 和 `results/rotation_v4_0_validation.md:7` 对应 50 股、136,476 行、2,887 个市场日（2015-01-05 至 2026-11-20），上市后缺行合计 0，指数缺日为 0。**既有记录**为 `stock_issues=0`、`etf_issues=0`、`production_replay=true`。真实双端回执载 128 条路径、150,400 行账本、64,134 笔成交；前缀于 2023-01-20 结束，96 条路径。独立运行了输入身份比对，没有重新下载行情或重演这些交易。
- ETF 7 只合计 11,466 行，冻结日按披露时点可用 11,459 行。下面的最后可用日是观测所属日期；冻结日观测按历史 T+1 约定到下一交易日才可用。所有“0 个问题”的表述都应结合这些分母阅读。

| ETF | 观测覆盖 | 记录行 | 冻结时可用行 | 最后可用观测 | 已覆盖区间缺日 |
|---|---|---:|---:|---|---:|
| 159915 | 2020-01-02 至 2026-11-20 | 1,667 | 1,666 | 2026-11-19 | 1（2021-02-08） |
| 159919 | 2020-01-02 至 2026-11-20 | 1,668 | 1,667 | 2026-11-19 | 0 |
| 510050 | 2020-01-02 至 2026-11-20 | 1,668 | 1,667 | 2026-11-19 | 0 |
| 510300 | 2020-01-02 至 2026-11-20 | 1,668 | 1,667 | 2026-11-19 | 0 |
| 510330 | 2020-01-02 至 2026-11-20 | 1,668 | 1,667 | 2026-11-19 | 0 |
| 510500 | 2020-01-02 至 2026-11-20 | 1,668 | 1,667 | 2026-11-19 | 0 |
| 588000 | 2020-11-16 至 2026-11-20 | 1,459 | 1,458 | 2026-11-19 | 0 |

## 工作区与核验记录

| 本次执行 | 实际退出码 | 结果与边界 |
|---|---:|---|
| Git 状态、HEAD、按目录/文件盘点，Python AST / JSON / JS 文本扫描 | 0 | 含 Git 未跟踪实验及报告目录内程序。单个补查 rg 使用 Windows 字面通配符时曾退出 1；随后以 Python 文件枚举和逐文件读取完成补查 |
| `.venv/Scripts/python.exe -B scripts/check_integrity.py` | 0 | 本机该虚拟环境为 Python 3.12.10；只对字节求哈希。五个批准对象均匹配 |
| 内存负例与表达式核验（Python 3.12.10，关闭字节码写入） | 0 | 复核 S01、S03、S04、S05、S08、S11。S03 临时在内存替代文件输出，执行了真实 evaluator 的解析、排名、锚点与最终返回路径。该实验的退出 0 是假绿证据 |
| 已存八张数据表、两项补充证据 SHA 比对；报告与 HTML 检查 | 0 | 逐项比对当前输入，复核已有统计的身份；未独立复算收益 |
| 本次 CI、完整合成套件、正式 Python 3.11/JS 回放、自然样本外及实际成交 | 未运行 | 合成测试会写 `results/synthetic_verification.json`；正式回放会更新页面与结果。本次任务仅审计。当前选池有回顾性选择偏差 |

**冻结引擎实测 SHA-256**

```text
090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80  engine.py
26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945  offline/engine.js
```

**原工作树保护。** 开始时有 16 个已跟踪文件带修改，另有未跟踪研究、底稿和报告；未清理或还原。原 22,889 个路径的顺序、字节数、mtime_ns 和非缓存/非字节码/非凭据文件的内容 SHA 联合指纹为 `45d95b8322c81f392d5dcf74c115c8742f01c358abe413f88a966018cc3c9f59`。开始与复核一致。原范围 `git --no-optional-locks status --porcelain=v1 -uall` 输出 SHA 为 `d3c59d26f45f4a8a822e8d5c769ef1f31dbd64631d99eb2d57bc04d4538aefc1`，前后相等。缓存、字节码与凭据仅对元数据核验；未声称逐字节重读那 1 GB 缓存。

**审计期间独立出现的文件。** `scripts/audit_model_reality.py` 和 `reports/model_reality_audit/` 下的 8 份产物在初始 Git 状态和物理盘点中均不存在。这些文件不是本次审计写入；按补扫时点单列在 inventory 末尾。新增脚本还有一个 S04 实例（`scripts/audit_model_reality.py:619` 的 `assert sys.version_info[:2]==(3,11) and __debug__`，第 678 行生成 PASS 回执）。同时多出一份 AnnVol20 诊断重算（第 234 行）和账户指标重算（第 121–124 行），所以若按含补扫脚本的工作树计，波动序列有 2 处、账户高水位有 6 处、研究 Calmar 有 3 处；交易账本循环仍是 5 个。补扫时现存脚本 SHA-256 为 `473ce0a1663c46107437ad227a0bea9fdae1a470cfbd90141b490f8cf0f2c449`，其既有 `reports/model_reality_audit/audit_manifest.json:5` 所载脚本为 `9732484cf35bf3dfe6dd48f945a9fae5cfc384ba91a7e7efdc94ab8a71ef6545`。该 PASS 只能归属于回执标识的版本；未重跑这个外部任务。

本次写入仅有本目录下的报告及两个 CSV。未生成可选辅助脚本，未执行数据库、远端或交易操作。

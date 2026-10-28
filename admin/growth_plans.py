r"""成长任务的「难度分级 + 完成策略」。

这里沉淀的是实测结论，不是猜测：

  已实测可自动完成的（走 chat/completions 的 growthEvent）：
    chat_5               <- chat_request_send
    automation_1         <- automated_task_create_suc
    skill_1              <- skill_info
    Model_chat_GLM5.2    <- 请求体 model 必须真的是 glm-5.2

  已实测可自动完成的（走 POST /v2/report 上报真实业务事件）：
    Library_read         <- web 域 web_element_click（+100 已实测到账）
    playbook_prompt      <- billing 域 playbook_prompt_send（+100 已实测到账）
    create_canvas        <- chat 域 wbx_design_canvas_task_create/open
    —— 这两个不吃 growthEvent，必须带完整业务字段与对应域的指纹头，
       走 chat 事件包无论发多少次都不计数。

  create_canvas 的特别说明：
    最早的结论是「事件里要自造 wb-<ms> 画布 id，属伪造业务对象，不做」。
    但官方源码给出了真实 id 的口径 —— **fileId 是纯数字**
    （正则 ``\bfileId\s+(\d+)\b``，URL 路径 ``/file/(\d+)``），由 MCP
    create_design 工具返回。所以正确做法不是编一个 id，而是**真的发一次
    设计请求**（调 Ardot MCP），让上游创建画布并回传 fileId，再用这个真实
    id 上报遥测。见 AccountSession.fire_design_canvas。

  已实测「发事件包无效」的（枚举 1131 个候选事件均未命中）：
    expert_5 / Hp_Appearance / template_5 / Buddy_App / Expert_lighthouse
    Expert_team_use_3 / RichMeow_Chat
    —— 归为 MANUAL。这些任务需要真实对象 id（专家 id / 模板 id），
       自造 id 属于伪造业务对象，后端核对即露，故暂不纳入自动执行。

  奖励为 0 或依赖支付/登录/三方的：直接跳过。
    Expert_Philanthropy 需真实捐款；black_cat 奖励为 0。

分级说明：
  AUTO   一次事件即可完成（简单，优先执行）
  MULTI  需要重复 N 次（如 chat_5 需 5 次）
  MANUAL 暂未找到安全的自动化方式（需人工在客户端/网页操作）
  SKIP   明确不处理（奖励为 0、需支付、需三方授权等）
"""
from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urlsplit

# 难度等级
AUTO = "auto"        # 可直接自动完成
MULTI = "multi"      # 需重复触发
MANUAL = "manual"    # 暂不可自动
SKIP = "skip"        # 跳过

LEVEL_LABEL = {
    AUTO: "简单",
    MULTI: "简单(多次)",
    MANUAL: "复杂",
    SKIP: "跳过",
}


@dataclass
class TaskPlan:
    """单个任务的完成策略。"""
    code: str
    level: str
    event_codes: list[str] = field(default_factory=list)
    reason: str = ""
    # 需要触发的次数；None 表示按任务 target 动态决定
    times: int | None = None
    # 指定模型：非空时用该模型真实调用（用于「体验某模型」类任务）
    model: str = ""
    # 触发方式：空 = 走 chat/completions 的 growthEvent；
    # 非空 = 调用 AccountSession 上的同名方法（如 fire_library_read）。
    # 有些任务不吃 growthEvent，要求客户端上报真实业务事件，必须分开走。
    firer: str = ""
    # 由任务元数据解析出的业务目标；只传给本地登记的触发器。
    firer_kwargs: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """是否可由本系统自动完成。

        三种触发方式都算「可自动」：
          * 有 event_codes（走 chat/completions 的 growthEvent）
          * 有 firer（走 POST /v2/report 上报真实业务事件）
          * 有 model（用该模型真实调一次对话，用于「体验某模型」类任务）
        少判一种就会出现「明明能自动做，面板却显示不可自动」——
        Model_chat_GLM5.2 就是这种情况（它只靠 model 字段触发）。
        """
        return self.level in (AUTO, MULTI) and bool(
            self.event_codes or self.firer or self.model)


#: 任务策略表。键为 task_code。
#: 未列出的任务默认按 MANUAL 处理（宁可让管理员手动，也不盲目发包）。
TASK_PLANS: dict[str, TaskPlan] = {
    # ---------------- 已实测可自动 ----------------
    "chat_5": TaskPlan(
        "chat_5", MULTI, ["chat_request_send"],
        "对话类：发送带 growthEvent 的对话请求，按 target 次数重复",
    ),
    "automation_1": TaskPlan(
        "automation_1", AUTO, ["automated_task_create_suc"],
        "自动化任务：创建成功事件（已实测命中）",
    ),
    "skill_1": TaskPlan(
        "skill_1", AUTO, ["skill_info"],
        "尝鲜技能：skill_info 事件（已实测命中）",
    ),

    # ---------------- 已实测可自动（需指定模型）----------------
    "Model_chat_GLM5.2": TaskPlan(
        "Model_chat_GLM5.2", AUTO, ["chat_request_send"],
        "模型体验：请求体 model 必须真的是 glm-5.2（发事件包无效，已实测）",
        model="glm-5.2",
    ),

    # ---------------- 已实测发事件包无效 ----------------
    "RichMeow_Chat": TaskPlan(
        "RichMeow_Chat", MANUAL, [],
        "桌面端对话：实测 chat_request_send 等 5 个事件均不计数，"
        "上游按客户端类型判定，需真实桌面端对话",
    ),
    "expert_5_paid": TaskPlan(
        "expert_5_paid", MANUAL, [],
        "召唤专家（付费版）：同上",
    ),
    # 已实测可自动完成（走 POST /v2/report 上报真实业务事件，
    # 不是 growthEvent —— 这些任务不吃事件包）
    # 对象 id 一律从官方接口现拉，拉不到就跳过，绝不编造。
    "playbook_prompt": TaskPlan(
        "playbook_prompt", AUTO, [],
        "灵感案例：billing 域上报 playbook_prompt_send（已实测点亮 +100）",
        firer="fire_playbook_prompt",
    ),
    "Library_read": TaskPlan(
        "Library_read", AUTO, [],
        "资料库：web 域上报资料库介绍页点击（已实测点亮 +100）",
        firer="fire_library_read",
    ),
    "expert_5": TaskPlan(
        "expert_5", MULTI, [],
        "召唤专家：用市场真实专家 id 上报 expert_actual_use（已实测点亮 +100）",
        firer="fire_expert_use",
    ),
    "template_5": TaskPlan(
        "template_5", MULTI, [],
        "使用模板：用真实场景 id 上报 template 事件（已实测点亮 +100）",
        firer="fire_template_use",
    ),
    "Expert_lighthouse": TaskPlan(
        "Expert_lighthouse", AUTO, [],
        "腾讯轻量云专家：关键词筛真实专家后上报（已实测点亮 +100）",
        firer="fire_lighthouse_expert",
    ),
    "Hp_Appearance": TaskPlan(
        "Hp_Appearance", AUTO, [],
        "和平精英主题：用真实主题 resourceKey 上报换肤（已实测点亮 +100）",
        firer="fire_appearance_skin",
    ),
    # 桌面端事件链。任务说明写的「需升级到 5.5.3+」是**客户端侧**门槛，
    # 服务端只认事件本身 —— 实测直接上报事件链即可完成，无需真装桌面端。
    # 事件必须带桌面指纹（见 AccountSession._desktop_fingerprint）。
    "Buddy_App": TaskPlan(
        "Buddy_App", AUTO, [],
        "发现应用：桌面指纹上报 buddyapp 五连事件（已实测点亮 +100）",
        firer="fire_buddy_app",
    ),
    "Buddy_App_QQ": TaskPlan(
        "Buddy_App_QQ", AUTO, [],
        "企鹅教师助手：同一组 buddyapp 五连事件（已实测点亮 +50）",
        firer="fire_buddy_app",
    ),
    "RichMeow_Chat": TaskPlan(
        "RichMeow_Chat", AUTO, [],
        "桌面端对话：桌面指纹上报 6 连对话事件链（已实测点亮 +100）",
        firer="fire_desktop_chat",
    ),
    "Expert_team_use_3": TaskPlan(
        "Expert_team_use_3", MULTI, [],
        "召唤专家团：expert_type=team 过滤取真实团队后上报（已实测点亮 +100）",
        firer="fire_expert_team",
    ),
    "Expert_Philanthropy": TaskPlan(
        "Expert_Philanthropy", MANUAL, [],
        "公益专家：需真实捐款动作，无法代做",
    ),
    "create_canvas": TaskPlan(
        "create_canvas", AUTO, [],
        "设计创意画布：调 Ardot MCP create_design 真实创建画布，用返回的真实纯数字"
        " fileId 上报 wbx_design_canvas_task_create/open；拿不到真实 id 就如实跳过"
        "（绝不伪造）。实测已点亮：1/1、+300 分。",
        firer="fire_design_canvas",
    ),

    # ---------------- 明确跳过 ----------------
    "black_cat": TaskPlan(
        "black_cat", SKIP, [],
        "奖励为 0（夜猫子折扣活动），无收益",
    ),
}


# ---------------------------------------------------------------------------
# 模式规则：上游会不断新增任务，且同一个任务会换档位
# ---------------------------------------------------------------------------
# TASK_PLANS 是「精确 code -> 策略」，只有实测过的具体任务才登记。
# 但上游的活动是会滚动的：
#   * 新增同类任务      如又来一个模板任务 template_3
#   * 同名任务换档位    如 chat_5 变 chat_10、template_5 变 template_3
# 只靠精确匹配的话，这些新 code 会全部落到 MANUAL —— 明明能做却不做，
# 而且**不会有任何报错**，只能靠人肉发现，这是最糟的失败方式。
#
# 所以再加一层模式匹配：命中规则就用现成的 firer 去做。
# 规则只覆盖「实测验证过的触发方式」，措辞按上游真实命名规律归纳：
#   chat_5 / chat_10 / chat_20           对话次数档位
#   template_5 / template_3              模板次数档位
#   expert_5 / expert_10                 召唤专家次数档位
#   Expert_team_use_3                    专家团次数档位
#   Model_chat_<模型名>                   模型体验
#   Hp_Appearance / *Appearance*         换肤类
#   *lighthouse*                         轻量云专家
#
# 注意：模式匹配到**已实测的触发方式**才用；匹配不出来的一律维持 MANUAL，
# 宁可不动也不盲发 —— 没人验证过的任务类型，猜错等于往上游灌垃圾事件。

#: (正则, 策略工厂)。工厂统一收 (code, match)，顺序敏感：先匹配到的先用。
PATTERN_RULES: list[tuple[str, callable]] = [
    # 对话次数档位：chat_5 / chat_10 ...
    (r"^chat_\d+$",
     lambda c, m: TaskPlan(c, MULTI, ["chat_request_send"],
                           "对话类（模式匹配）：发带 growthEvent 的对话请求，按 target 重复")),
    # 模型体验：Model_chat_GLM5.2 / Model_chat_GPT5 ...
    (r"^Model_chat_(.+)$",
     lambda c, m: TaskPlan(c, AUTO, [],
                           "模型体验（模式匹配）：请求体 model 必须是真的对应模型",
                           times=1,
                           model=MODEL_ALIASES.get(m.group(1), m.group(1).lower()))),
    # 模板次数档位：template_5 / template_3 ...
    (r"^template_\d+$",
     lambda c, m: TaskPlan(c, MULTI, [],
                           "使用模板（模式匹配）：用真实场景 id 上报（+100）",
                           firer="fire_template_use")),
    # 召唤专家次数档位：expert_5 / expert_10 ...
    (r"^expert_\d+$",
     lambda c, m: TaskPlan(c, MULTI, [],
                           "召唤专家（模式匹配）：用市场真实专家 id 上报（+100）",
                           firer="fire_expert_use")),
    # 专家团：Expert_team_use_N
    (r"^Expert_team_use_\d+$",
     lambda c, m: TaskPlan(c, MULTI, [],
                           "召唤专家团（模式匹配）：expert_type=team 过滤后上报（+100）",
                           firer="fire_expert_team")),
    # 轻量云专家：Expert_lighthouse ...
    (r"^Expert_lighthouse",
     lambda c, m: TaskPlan(c, AUTO, [],
                           "轻量云专家（模式匹配）：关键词筛真实专家后上报（+100）",
                           firer="fire_lighthouse_expert")),
    # 换肤：Hp_Appearance / Hp_Appearance_2 / appearance_theme / Xx_Appearance
    # 用 search 而不是 match —— code 可能以别的词开头（Hp_...），
    # 用 match 会漏掉，而漏掉的后果是「能做却不做」且不报错。
    (r"(?i)appearance",
     lambda c, m: TaskPlan(c, AUTO, [],
                           "主题换肤（模式匹配）：用真实主题 resourceKey 上报（+100）",
                           firer="fire_appearance_skin")),
    # 技能：skill_1 / skill_5 ...（实测 skill_info 事件可用）
    (r"^skill_\d+$",
     lambda c, m: TaskPlan(c, AUTO, ["skill_info"],
                           "尝鲜技能（模式匹配）：skill_info 事件（+100）")),
    # 自动化任务：automation_1 / automation_3 ...
    (r"^automation_\d+$",
     lambda c, m: TaskPlan(c, AUTO, ["automated_task_create_suc"],
                           "自动化任务（模式匹配）：创建成功事件（+100）")),
]

#: 模型 code -> 真实请求用的 model id。上游任务名用的是营销名，
#: 请求体里必须是真的模型标识。
MODEL_ALIASES = {
    "GLM5.2": "glm-5.2",
    "GLM4.6": "glm-4.6",
    "GPT5": "gpt-5",
    "DeepSeekV4": "deepseek-v4-flash",
}

#: 模式匹配务必避开的 code —— 这些看着像某类，但实测不可做或另有语义。
PATTERN_DENY = {
    "expert_5_paid",       # 付费版，无收益
}


def _expert_jump(raw_url: str):
    """解析任务跳转里的专家 deeplink。

    两种形态都要认：
      * 原生 ``workbuddy://expert`` / ``workbuddy://experts?expertId=ex_...``
      * 网页启动页 ``https://www.workbuddy.cn/work/launch/?deeplink=<urlencoded>``
        —— 上游把真正的 deeplink 塞在 query 里，只看外层 scheme 会漏掉
        整个 expert_use_* 任务族（2026-10-24 实测踩坑）。
    返回解析后的 urlsplit；不是专家跳转返回 None。
    """
    try:
        jump = urlsplit(raw_url)
    except ValueError:
        return None
    if jump.scheme == "workbuddy" and jump.netloc in ("expert", "experts"):
        return jump
    if jump.scheme in ("http", "https"):
        dl = (parse_qs(jump.query).get("deeplink") or [""])[0]
        try:
            inner = urlsplit(dl)
        except ValueError:
            return None
        if inner.scheme == "workbuddy" and inner.netloc in ("expert", "experts"):
            return inner
    return None


#: 标题/描述里可能被引号包住的通用词，不是专家名，提取时排除。
_QUOTE_STOPWORDS = {"专家", "模板", "新建任务", "专家页"}


def _named_expert_plan(task_code: str, task: dict) -> TaskPlan | None:
    """识别指定专家体验活动，不依赖活动码或专家名称白名单。

    判定依据：跳转到专家页（原生或 launch 页内嵌 deeplink）+ 文案明确
    要求召唤并对话 + 标题与描述里被同一对引号包住的唯一名字。
    描述不明确或带支付/授权等额外条件时交给人工，不能随机选专家。

    deeplink 里带的 ``expertId`` 是上游自己指定的活动专家 id，
    属官方元数据而非自造 —— 一并传给触发器优先使用（2026-10-24：
    活动专家实测不在市场列表里，仅按名称搜会 0 个或多个匹配）。
    """
    title = str(task.get("title") or "")
    desc = "\n".join(str(task.get(k) or "") for k in ("description", "task_desc"))
    jump = _expert_jump(str(task.get("jump_url") or ""))
    if jump is None:
        return None
    text = title + "\n" + desc
    if not re.search(r"召唤|使用|体验", text) or "对话" not in text:
        return None
    if re.search(r"付费|支付|购买|捐款|充值|授权|订阅|邀请|分享|关注", text):
        return None
    quoted = lambda s: {x.strip() for x in
                        re.findall(r'[「“\"]([^」”\"\n]{2,100})[」”\"]', s)}
    names = (quoted(title) & quoted(desc)) - _QUOTE_STOPWORDS
    if not names:
        names = {x.strip() for x in quoted(title + "\n" + desc)
                 if x.strip().endswith("专家") and x.strip() != "专家"}
    if len(names) != 1:
        return None
    name = names.pop()
    kwargs = {"keyword": name}
    expert_id = (parse_qs(jump.query).get("expertId") or [None])[0]
    if expert_id:
        kwargs["expert_id"] = expert_id
    return TaskPlan(task_code, AUTO, reason=f"指定专家体验：按名称查找真实专家「{name}」并上报，以上游进度确认完成",
                    firer="fire_expert_use", firer_kwargs=kwargs)


def plan_for(task_code: str, task: dict | None = None) -> TaskPlan:
    """取任务策略。

    顺序很重要：
      1. 先查精确表 TASK_PLANS（实测过的具体任务，含特殊形状）
      2. 再试模式规则 PATTERN_RULES（覆盖同类的未来新任务与换档位）
      3. 都没有 -> MANUAL（宁可不做，也不盲发未验证的事件）
    """
    hit = TASK_PLANS.get(task_code)
    if hit:
        return hit
    if task_code in PATTERN_DENY:
        return TaskPlan(task_code, MANUAL, [], "已明确不做（付费/无收益）")
    if task:
        named = _named_expert_plan(task_code, task)
        if named:
            return named
    for pattern, factory in PATTERN_RULES:
        # 用 search：code 可能带前缀（Hp_Appearance_2 / YY_appearance_new），
        # 用 match 只从开头比，会漏掉这些变体。漏掉的代价是「能做却不做」
        # 且完全静默，比多匹配更糟。规则本身已写 ^ 锚定它们要锚定的部分。
        m = re.search(pattern, task_code)
        if m:
            return factory(task_code, m)
    return TaskPlan(task_code, MANUAL, [], "未登记的任务类型，需人工确认")


def classify(task: dict) -> dict:
    """给任务补充分级信息，供前端展示。"""
    code = task.get("task_code") or ""
    plan = plan_for(code, task)
    reward = task.get("reward_credit") or 0

    level = plan.level
    # 奖励为 0 的一律降级为跳过（策略表可能滞后于上游活动）
    if reward <= 0 and level != SKIP:
        level = SKIP
    if task.get("locked"):
        level = MANUAL

    progress = task.get("progress") or {}
    target = progress.get("target")
    current = progress.get("current") or 0

    return {
        **task,
        "level": level,
        "level_label": LEVEL_LABEL.get(level, "未知"),
        # 直接复用 TaskPlan.actionable，不要再重复写一遍判断条件——
        # 之前这里单独判了 event_codes，导致「用 firer 上报」和「靠 model
        # 触发」的任务明明能自动完成，面板上却显示不可自动
        "actionable": plan.actionable and level in (AUTO, MULTI),
        "strategy": plan.reason,
        # 待触发次数：优先用任务自带 target，否则用策略表，最后兜底 1
        "need_times": (target - current) if isinstance(target, int) and target > current
                      else (plan.times or 1),
    }

"""运行时配置：浏览器路径、ffmpeg 路径、抓取与改写参数。

原先这些参数散落在 config.py 的模块常量里，改一次要动代码 + 重启；
这里把它们落到 SQLite 的 app_settings 表，由「系统设置」页维护，
其它模块在运行时通过 get_setting() / resolve_binary() 取用。

约定：
- 预置项（PRESETS）只在缺失时插入，绝不覆盖用户已改的值；
- value 一律按字符串存，按需用 get_int / get_bool / get_float 转换；
- is_secret=1 的项（密钥类）列表接口只回掩码，保存时空值表示「不修改」。
"""
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import get_conn, write_conn

# 设置分组（页面左侧/上方分区展示）
CATEGORIES: List[Dict[str, Any]] = [
    {"key": "browser", "title": "浏览器", "desc": "无头浏览器用于渲染 JS 页面、登录态抓取与截图"},
    {"key": "media", "title": "媒体处理", "desc": "ffmpeg / ffprobe 可执行文件位置与并发"},
    {"key": "fetch", "title": "抓取网络", "desc": "原文抓取的并发、超时与 UA"},
    {"key": "rewrite", "title": "内容改写", "desc": "改写轮次、字数分流、重试与差异化的默认行为（提示词见「提示词模板」）"},
    {"key": "sensitive", "title": "敏感词校验", "desc": "润色后校验：词库分级、命中词修复轮次与交付闸门"},
    {"key": "publish", "title": "发布分发", "desc": "各平台草稿箱接口；平台改版时在这里换端点，不用改代码"},
    {"key": "prompt", "title": "提示词模板", "desc": "按「平台 · 内容形态」分块维护提示词；新增平台只增加块，不影响其它平台"},
    {"key": "layout", "title": "文章排版", "desc": "小标题识别、素材挑选与插入位置规则"},
    {"key": "style", "title": "来源标记", "desc": "把原文「来源」列归一成头条 / 百家，仅用于标记筛选，不限制产出与发布"},
]

_YES_NO = [{"value": "1", "label": "是"}, {"value": "0", "label": "否"}]
_HEADED = [{"value": "headless", "label": "无头（后台运行）"}, {"value": "headed", "label": "有头（可见窗口）"}]

# 改写提示词默认值。{{article_text}} 是唯一的正文占位符：
# 第一轮塞原文，第二轮塞第一轮的输出。
PROMPT_ROUND1 = """你是一名专业社会资讯编辑，对下面的新闻稿件进行深度改写。

【硬性强制规则，必须全部遵守，禁止违反】
1. 事实绝对不能改动：事发时间、地点、人物、事件经过、警方通报内容、数字信息，全部原样保留，严禁新增、删减、脑补任何情节，不能主观推断、猜测案件结果。
2. 禁止简单同义词替换。重新组织句子结构，调整句式长短，部分段落可以调换顺序，换一套表达方式复述事件。
3. 写作风格：客观中立的民生新闻口吻，去掉夸张、情绪化、煽动性词汇。尚未宣判的案件，不使用“罪犯、恶魔”这类定性词语。
4. 段落：一段一个自然段，段落之间空一行，使用Markdown格式输出正文。
5. 不要使用模板化、工整刻板的书面套话，尽量贴近真人媒体撰稿习惯，减少AI写作特征。
6. 不要输出解释、说明、前言、后记，**只输出Markdown正文，不要html标签**。
7. 字数控制：改写后篇幅与原文差距控制在±15%以内，不能为凑字数增加虚假内容，也不能删减案件关键信息。
8. ⚠️ 不要在文中自行添加图片、图片描述，文章内只写文字段落。图片插入是后续程序自动处理，你只输出纯文本Markdown。
9. 降重：不得出现与原文连续 10 个字以上相同的表述；判决书、通报、当事人原话必须转述（换主语、换句式、合并或拆分），禁止整段引用；段落顺序可按叙事逻辑重排。平台查重按连续相同片段判定，照抄一整句就会被打上「高度相似」。

原文：
{{article_text}}"""

PROMPT_TITLE = """你是自媒体资讯编辑，基于下面这篇新闻原文，生成多组新闻主标题。

硬性规则：
1. 标题内容必须完全忠于原文事实，严禁编造、夸大案情，不渲染血腥、不使用煽动性词汇。
2. 尚未判决的案件，不使用“罪犯、恶魔”这类定性词语。
3. 生成3个不同风格标题：
标题1（头条风格）：简洁，抓住事件核心，适合信息流阅读，控制20~28字
标题2（百家号风格）：偏陈述、完整，利于搜索引擎收录，25~35字
标题3（知乎风格）：客观陈述，偏理性，28~38字
4. 只输出标题，不要多余解释，一行一条，不要编号以外的文字。

原文：
{{article_text}}"""

PROMPT_ROUND2 = """下面这篇稿件已经完成改写，现在做自然化润色，降低AI痕迹。

硬性规则：
1. 所有事实、时间、地点、人物、案情、数字一字不动，绝对不能修改事件信息，不新增内容。
2. 打散过于整齐的长句，长短句穿插；删减AI常见的过渡套话（诸如“值得一提的是、据了解、由此可见”这类模板词汇）。
3. 保持新闻客观中立，不要添加主观评价，不渲染暴力。
4. 保留原有段落结构，段落之间保留空行，输出Markdown格式，不要HTML标签。
5. 字数变化控制在原文±5%以内，不要大幅度增减篇幅。
6. 不要增加图片相关描述，不要插入图片标记。
7. 只输出润色完成后的正文，不要任何额外解释。
8. 额外风格要求：适合头条、百家号自媒体阅读习惯，开头简洁抓住重点，语句通俗易懂，句子不宜过长，保持客观，不夸张博眼球。
9. 字数控制：改写后篇幅与原文**差距控制在 ±15% 以内**，不要大幅扩写、不要大量删减；原文关键信息不能为了凑字数增删（举例：原文 800 字，改写后允许 680～920 字）

待润色稿件：
{{article_text}}"""

# 微头条：没有标题，篇幅短、口语化。单独一套模板，避免和图文稿混用。
PROMPT_MICRO_BODY = """你是一名头条微头条作者，把下面这篇新闻改写成一条微头条。

硬性规则：
1. 事实绝对不能改：事发时间、地点、人物、事件经过、警方通报内容、数字信息全部原样保留，严禁新增、删减、脑补情节。
2. 微头条没有标题，也没有小标题：直接写正文，第一句就抛出事件结果或冲突，不要写“导语 / 据悉 / 近日”这类起手式。
3. 篇幅控制在 150~300 字，分成 2~4 个短段，一段 1~2 句；口语化、短句，像人在手机上说话。
4. 去掉夸张、情绪化、煽动性表达；尚未宣判的案件不使用“罪犯、恶魔”这类定性词。
5. 不要输出任何 Markdown 标记（#、**、列表），不要输出标题、配图说明、话题标签。
6. 只输出微头条正文，不要解释、不要前言后记。

原文：
{{article_text}}"""

PROMPT_MICRO_POLISH = """下面是一条微头条初稿，做自然化润色，降低 AI 痕迹。

硬性规则：
1. 事实、时间、地点、人物、数字一字不动，不新增任何信息。
2. 保持 150~300 字，仍是 2~4 个短段；不要加标题、小标题、Markdown 标记、话题标签。
3. 打散过于整齐的长句，删掉“值得一提的是 / 由此可见 / 据了解”这类套话，更像真人口播。
4. 只输出润色后的正文，不要解释。

待润色微头条：
{{article_text}}"""

# 百家号动态：和微头条同级（无标题、篇幅短），但语气更偏信息流短资讯，不口播。
PROMPT_BJH_MICRO_BODY = """你是一名百家号动态作者，把下面这篇新闻改写成一条百家号动态。

百家号动态 = 信息流里的短资讯：没有标题，也没有小标题，读者是在刷信息流时看到它的。

硬性规则：
1. 事实绝对不能改：事发时间、地点、人物、事件经过、通报内容、数字信息全部原样保留，严禁新增、删减、脑补情节，不能推断案件结果。
2. 第一句直接交代“谁 + 做了什么 + 结果”，不要“据悉 / 近日 / 随着”这类起手式，也不要写导语。
3. 篇幅 120~260 字，分 2~4 个短段，一段 1~2 句；句子短、少从句，读完一段就能拿到一个信息点。
4. 语气：信息流资讯口吻，客观克制，不用感叹号堆情绪，不用“震惊 / 炸裂 / 全网都在传”这类博眼球表达。
5. 只用原文有的事实；原文没提的背景不要补，不要写“据网友猜测 / 有分析认为”这类无源表述。
6. 不要输出标题、小标题、Markdown 标记（#、**、列表）、话题标签、配图说明，也不要输出解释、前言、后记。

原文：
{{article_text}}"""

PROMPT_BJH_MICRO_POLISH = """下面是一条百家号动态初稿，做自然化润色，降低 AI 痕迹。

硬性规则：
1. 事实、时间、地点、人物、数字一字不动，不新增、不删减任何信息。
2. 保持 120~260 字、2~4 个短段；不要补标题、小标题、Markdown 标记或话题标签。
3. 打散过于整齐的句子，删掉“值得一提的是 / 由此可见 / 据了解”这类模板过渡词，读起来像真人在发资讯。
4. 去掉夸张和情绪化表达，保持客观，不渲染暴力。
5. 只输出润色后的正文，不要任何解释。

待润色动态：
{{article_text}}"""

# 降重重写的初版模板（仅用于把老库里的默认值迁移掉，不要再使用）
PROMPT_DEDUP_V1 = """下面这篇稿件已经改写完成，但与原文仍有较多连续相同的表述，发布后会被平台判定「与站内现有内容高度相似」，限制推荐甚至仅主页可见。

【下面这些句子与原文几乎逐字相同，必须重写】
{{fragments}}

【硬性规则，必须全部遵守】
1. 事实绝对不能改动：事发时间、地点、人物、事件经过、通报内容、数字信息一字不动；严禁新增、删减、脑补情节。
2. 上面列出的每一句都要换一种说法：换主语、换语序、换叙述角度（可以从结果切入、从当事人视角切入、从法院认定切入），或与相邻句合并、拆分。
3. 改写后不得与原文出现连续 8 个字以上完全相同的表述；判决书、警方通报、当事人原话必须转述，禁止整段引用。
4. 段落顺序可以按叙事逻辑重排（例如先给判决结果再补案情），不要保留原文的段落骨架。
5. 不要为了降重删掉关键事实，也不要编造细节凑长度；篇幅变化控制在 ±10% 以内。
6. 保持原有段落结构（段落之间空一行），输出 Markdown 正文，不要标题、不要图片、不要任何解释或前言后记。
7. 只输出修改后的正文全文。

待降重稿件：
{{article_text}}"""

# 降重重写的基础模板（程序内置，随版本迭代；设置页里的「降重附加要求」会追加在后面）。
# 平台（尤其百家号）判「与站内现有内容高度相似」看的是连续相同片段，
# 所以这里刻意要求换叙述框架整体重写，而不是逐句替换同义词。
PROMPT_DEDUP_BASE = """你在做「降重改写」，目标是让成稿与原文不再有连续 8 个字以上相同的表述，避开平台查重（百家号会判「与站内现有内容高度相似」并限制推荐）。

【原文（只用来对照事实，禁止照抄任何句子）】
{{origin_text}}

【当前稿件里与原文几乎逐字相同的句子（必须彻底重写）】
{{fragments}}

【改写方法，必须照做】
1. 不要逐句替换同义词——那样还是会撞查重。请换一个叙述框架重写整篇：
   - 换切入顺序：可以先给判决结果/结论再倒叙案情，也可以从最有冲突感的现场细节切入；
   - 换段落骨架：原文按时间线写就按责任划分写，原文按当事人视角写就按法院认定视角写；
   - 合并或拆分句子，让句长、主被动、语序都与原文不同。
2. 判决书、警方通报、当事人原话不能整段引用，必须转述或概括（例如「法院认定司机中途甩客存在过错」）；某句实在无法在不改事实的前提下改写时，就把它压缩成一句概括，只留结论，不要保留原句。
   示范（错误做法 → 正确做法）：
   原文：「综合全案过错程度与因果关系，法院一审判决覃某赔偿8000元，某租赁公司南宁分公司赔偿5000元，其余损失由原告自行承担。」
   照抄（错）：「综合全案过错程度与因果关系，法院一审判决覃某赔偿8000元……」
   转述（对）：「最终，覃某被判赔 8000 元，他挂靠的租赁公司再赔 5000 元，剩下的损失由家属自己承担。」
3. 公司全称、法条条文、金额这些必须保留的事实，也要换一种句式承载，不要把原句结构照搬过来。
4. 事实绝对不能改：时间、地点、人物、经过、数字、责任认定一字不动；不新增、不删减、不脑补情节。
5. 篇幅与当前稿件相差 ±10% 以内。
6. 输出 Markdown 正文（段落之间空一行），不要标题、不要图片、不要任何解释或前言后记。
7. 只输出重写后的正文全文。

待降重稿件：
{{article_text}}"""

# 敏感词修复：润色稿命中拦截级敏感词后，把「命中清单 + 稿件」交回模型改写。
# {{hits}} = 程序生成的命中词清单（词 + 次数 + 上下文），{{article_text}} = 待修稿。
PROMPT_FIX = """下面这篇稿件已经完成改写与润色，但自检时命中了平台敏感词，需要在不改变事实的前提下清除。

【必须清除的敏感词（命中清单）】
{{hits}}

【硬性规则，必须全部遵守】
1. 事实绝对不能改动：事发时间、地点、人物、事件经过、通报内容、数字信息一字不动；严禁新增、删减、脑补情节，不能推断案件结果或改变案件定性。
2. 清除方式按优先级选择：
   ① 改写句子结构与措辞，用同义表达绕开该词；
   ② 必要时用上位概念概括（如具体毒品名 → 毒品，具体作案手法 → 违法行为）；
   ③ 实在无法绕开的细节可以删除该细节，但句子必须通顺、文意完整，不得留下语病、空缺或占位符。
3. 命中词若是机构名、职务名、地名、法律法规名称等**事实专有名称**（如「XX市政府」「XX办公厅」「某党派当局」），
   不要硬拆或删字，改为调整该分句结构、用中性指代替换（如「当地相关部门」「台湾地区有关方面」），保证指代清楚、事实不歪曲。
4. 严禁用拼音、谐音、拆字、加分隔符或符号等方式变相保留命中词；输出中不得再出现清单里的任何一个词。
5. 只修改命中词所在的句子，其余段落尽量原样保留；保持原有 Markdown 段落结构，段落之间空一行，不要 HTML 标签、不要标题、不要解释。
6. 保持客观中立的民生新闻口吻，不渲染暴力、不添加主观评价。
7. 篇幅变化控制在 ±8% 以内，不要为了绕开敏感词而大幅扩写或删减。
8. 只输出修改后的正文全文，不要任何前言、后记、说明。

待修改稿件：
{{article_text}}"""

PROMPT_MICRO_FIX = """下面这条短稿已经完成改写与润色，但自检时命中了平台敏感词，需要在不改变事实的前提下清除。

【必须清除的敏感词（命中清单）】
{{hits}}

【硬性规则】
1. 事实、时间、地点、人物、数字一字不动，不新增、不删减信息，不改变案件定性。
2. 清除方式按优先级：改写句子结构绕开该词 → 用上位概念概括 → 删除该细节并保证句子通顺。
3. 命中词若是机构名、职务名、地名等事实专有名称，不要硬拆或删字，用中性指代改写整个分句。
4. 严禁用拼音、谐音、拆字、加符号等方式变相保留；输出中不得再出现清单里的任何一个词。
5. 保持原有篇幅（短文 120~300 字）、2~4 个短段；不要补标题、小标题、Markdown 标记或话题标签。
6. 只改命中词所在的句子，其余尽量原样保留；只输出修改后的正文，不要任何解释。

待修改短稿：
{{article_text}}"""

# 分块提示词：平台 × 内容形态 × 阶段。顺序即页面左侧块的展示顺序。
# 以后接新平台：在 app/style.py 注册 FORMS，再往这里加一块即可。
PROMPT_BLOCKS: List[Dict[str, Any]] = [
    {"platform": "toutiao", "form": "article", "title": "头条文章",
     "hint": "原文 ≥ 短文阈值（默认 400 字）；需要标题，走图文发布入口，存草稿",
     "stages": {"title": PROMPT_TITLE, "body": PROMPT_ROUND1, "polish": PROMPT_ROUND2,
                "fix": PROMPT_FIX}},
    {"platform": "toutiao", "form": "micro", "title": "头条微头条",
     "hint": "原文 < 短文阈值（默认 400 字）；没有标题，篇幅短，只存微头条草稿（此时不产出头条图文）",
     "stages": {"body": PROMPT_MICRO_BODY, "polish": PROMPT_MICRO_POLISH,
                "fix": PROMPT_MICRO_FIX}},
    {"platform": "baijia", "form": "article", "title": "百家号图文",
     "hint": "原文 ≥ 短文阈值（默认 400 字）；需要标题，走百家号图文入口（此时不产出百家动态）",
     "stages": {"title": PROMPT_TITLE, "body": PROMPT_ROUND1, "polish": PROMPT_ROUND2,
                "fix": PROMPT_FIX}},
    {"platform": "baijia", "form": "micro", "title": "百家号动态",
     "hint": "原文 < 短文阈值（默认 400 字）；没有标题，信息流短资讯（走百家号动态入口，只存草稿，人工发布）",
     "stages": {"body": PROMPT_BJH_MICRO_BODY, "polish": PROMPT_BJH_MICRO_POLISH,
                "fix": PROMPT_MICRO_FIX}},
]
PROMPT_STAGE_LABEL = {"title": "标题改写", "body": "正文改写", "polish": "自然化润色",
                      "fix": "敏感词修复"}
#: 分块模板为空时的兜底（老版本的三条通用提示词 + 敏感词修复）
PROMPT_FALLBACK = {"title": "rewrite.prompt_title",
                   "body": "rewrite.prompt_round1",
                   "polish": "rewrite.prompt_round2",
                   "fix": "rewrite.prompt_fix"}

# 三轮提示词的输入各不相同，hint 写死，避免配错
_HINT_TITLE = ("输入 = 原文全文。必须带 {{article_text}}，模型要读到原文才能忠于事实；"
             "缺失变量时程序会把原文自动追加到提示词末尾")
_HINT_BODY = ("输入 = 原文全文（不是初稿）。{{article_text}} 替换为原文，"
             "已确定的标题会以附加约束追加在提示词后面")
_HINT_POLISH = ("输入 = 第 2 轮输出的初稿，不是原文。{{article_text}} 替换为初稿全文，"
                "这一轮只做降 AI 痕迹的润色")
_HINT_FIX = ("输入 = 润色后的稿件 + 命中词清单。{{article_text}} 是待修稿，{{hits}} 由程序生成"
             "（词 + 次数 + 上下文），模型必须在不动事实的前提下清除清单里的词")

# 预置项：key / value / category / label / hint / input_type / options / prober / is_secret / sort_order
PRESETS: List[Dict[str, Any]] = [
    # ---------------- 浏览器 ----------------
    {"key": "browser.executable", "value": "", "category": "browser", "label": "浏览器可执行文件",
     "hint": "如 C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe；留空自动从 PATH 探测",
     "input_type": "text", "prober": "file", "sort_order": 10},
    {"key": "browser.driver", "value": "", "category": "browser", "label": "驱动（chromedriver / msedgedriver）",
     "hint": "留空自动探测；Selenium 4+ 可只装 Selenium Manager",
     "input_type": "text", "prober": "bin", "sort_order": 20},
    {"key": "browser.mode", "value": "headless", "category": "browser", "label": "运行模式",
     "hint": "调试页面抓取时可临时切到有头", "input_type": "select", "options": _HEADED, "sort_order": 30},
    {"key": "browser.user_agent", "value": "", "category": "browser", "label": "自定义 User-Agent",
     "hint": "留空使用内置 UA", "input_type": "text", "sort_order": 40},
    {"key": "browser.timeout", "value": "30", "category": "browser", "label": "单页超时（秒）",
     "hint": "", "input_type": "number", "sort_order": 50},
    {"key": "browser.proxy", "value": "", "category": "browser", "label": "代理地址",
     "hint": "如 http://127.0.0.1:7890，留空直连", "input_type": "text", "sort_order": 60},
    {"key": "browser.download_dir", "value": "", "category": "browser", "label": "浏览器下载目录",
     "hint": "留空用系统默认下载目录", "input_type": "text", "prober": "dir", "sort_order": 70},
    # ---------------- 媒体处理 ----------------
    {"key": "media.ffmpeg", "value": "", "category": "media", "label": "ffmpeg 路径",
     "hint": "留空自动从 PATH 探测", "input_type": "text", "prober": "bin", "sort_order": 10},
    {"key": "media.ffprobe", "value": "", "category": "media", "label": "ffprobe 路径",
     "hint": "留空自动从 PATH 探测", "input_type": "text", "prober": "bin", "sort_order": 20},
    {"key": "media.concurrent", "value": "2", "category": "media", "label": "并发处理数",
     "hint": "同时处理的视频/图片数，过高会吃满 CPU", "input_type": "number", "sort_order": 30},
    {"key": "media.extra_args", "value": "", "category": "media", "label": "ffmpeg 额外参数",
     "hint": "追加到每次转码命令后，如 -threads 4", "input_type": "text", "sort_order": 40},
    # ---------------- 抓取网络 ----------------
    {"key": "fetch.concurrent", "value": "4", "category": "fetch", "label": "并发抓取数",
     "hint": "", "input_type": "number", "sort_order": 10},
    {"key": "fetch.timeout", "value": "20", "category": "fetch", "label": "请求超时（秒）",
     "hint": "", "input_type": "number", "sort_order": 20},
    {"key": "fetch.retries", "value": "2", "category": "fetch", "label": "失败重试次数",
     "hint": "", "input_type": "number", "sort_order": 30},
    {"key": "fetch.user_agent", "value": "", "category": "fetch", "label": "抓取 User-Agent",
     "hint": "留空使用内置 UA", "input_type": "text", "sort_order": 40},
    {"key": "fetch.proxy", "value": "", "category": "fetch", "label": "抓取代理",
     "hint": "如 http://127.0.0.1:7890，留空直连", "input_type": "text", "sort_order": 50},
    # ---------------- 内容改写 ----------------
    {"key": "rewrite.article_concurrency", "value": "2", "category": "rewrite", "label": "文章改写并发数",
     "hint": "", "input_type": "number", "sort_order": 20},
    {"key": "rewrite.max_retries", "value": "2", "category": "rewrite", "label": "单步失败重试次数",
     "hint": "标题/正文/润色每一步各自计数，重试仍失败才整篇判失败",
     "input_type": "number", "sort_order": 25},
    {"key": "rewrite.video_concurrency", "value": "1", "category": "rewrite", "label": "视频生成并发数",
     "hint": "视频链路吃资源，建议 1", "input_type": "number", "sort_order": 30},
    {"key": "rewrite.keep_title", "value": "0", "category": "rewrite", "label": "保留原标题",
     "hint": "开启则跳过标题改写、最终沿用原标题；关闭则跑第 1 轮标题改写，生成 3 个候选供挑选",
     "input_type": "select", "options": _YES_NO, "sort_order": 40},
    {"key": "rewrite.manual_review", "value": "1", "category": "rewrite", "label": "改写后需人工复核",
     "hint": "复核通过前不进入导出环节", "input_type": "select", "options": _YES_NO, "sort_order": 50},
    {"key": "rewrite.enable_polish", "value": "1", "category": "rewrite", "label": "执行第三轮润色",
     "hint": "关闭则正文改写后直接出稿", "input_type": "select", "options": _YES_NO, "sort_order": 60},
    {"key": "rewrite.diff_min", "value": "0.86", "category": "rewrite", "label": "平台版本差异阈值",
     "hint": "头条版与百家版正文重合度超过该值（0~1）时自动换角度重写一次，避免两篇几乎一样",
     "input_type": "number", "sort_order": 62},
    {"key": "rewrite.dedup_enabled", "value": "1", "category": "rewrite", "label": "与原文重合度检测 + 自动降重",
     "hint": "开启后每篇成稿都会与原文比一遍字面重合率，超过阈值就自动带着重复句清单回炉重写；"
             "平台（尤其百家号）判定「高度相似」看的就是连续相同片段",
     "input_type": "select", "options": _YES_NO, "sort_order": 64},
    {"key": "rewrite.dedup_ratio", "value": "0.20", "category": "rewrite",
     "label": "重合率阈值",
     "hint": "0~1，默认 0.20（20%）。超过就触发降重重写；平台查重严的可以调到 0.15 以下",
     "input_type": "number", "sort_order": 65},
    {"key": "rewrite.dedup_rounds", "value": "2", "category": "rewrite",
     "label": "降重重写最多轮数",
     "hint": "每轮多一次模型调用；事实性强、引文多的稿件建议 2 轮，0 表示只检测不重写",
     "input_type": "number", "sort_order": 66},
    {"key": "rewrite.dedup_minrun", "value": "13", "category": "rewrite",
     "label": "连续重合字数门槛",
     "hint": "连续多少个字完全相同才算重合，默认 13（整句级）；调小更严格，但机构名、常用短语会被算进去",
     "input_type": "number", "sort_order": 67},
    {"key": "rewrite.prompt_dedup", "value": "", "category": "rewrite",
     "label": "降重附加要求（可选）",
     "hint": "基础降重规则已内置（对照原文换叙事框架重写、禁止整段引用）。这里填的补充要求会追加在后面；"
             "留空即只用内置规则",
     "input_type": "prompt", "sort_order": 68},
    {"key": "rewrite.micro_max_chars", "value": "400", "category": "rewrite",
     "label": "短文阈值（微头条 / 百家动态）",
     "hint": "原文正文去掉空白后少于该字数 → 只产出短稿：头条微头条 + 百家号动态（无标题、篇幅短）；"
             "达到或超过 → 只产出图文：头条图文 + 百家号图文（需标题）。另一组形态不改写、不排版",
     "input_type": "number", "sort_order": 63},
    # 以下三条是改造前的通用提示词：仅当「提示词模板」里对应分块为空时兜底，
    # 保留是为了不丢用户历史自定义内容。
    {"key": "rewrite.prompt_title", "value": PROMPT_TITLE, "category": "rewrite",
     "label": "旧·通用标题提示词（兜底）",
     "hint": _HINT_TITLE + "；现在优先用「提示词模板 → 平台块 → 标题改写」",
     "input_type": "prompt", "sort_order": 90},
    {"key": "rewrite.prompt_round1", "value": PROMPT_ROUND1, "category": "rewrite",
     "label": "旧·通用正文提示词（兜底）",
     "hint": _HINT_BODY + "；现在优先用对应平台块的正文提示词",
     "input_type": "prompt", "sort_order": 91},
    {"key": "rewrite.prompt_round2", "value": PROMPT_ROUND2, "category": "rewrite",
     "label": "旧·通用润色提示词（兜底）",
     "hint": _HINT_POLISH + "；现在优先用对应平台块的润色提示词",
     "input_type": "prompt", "sort_order": 92},
    {"key": "rewrite.prompt_fix", "value": PROMPT_FIX, "category": "rewrite",
     "label": "旧·通用敏感词修复提示词（兜底）",
     "hint": _HINT_FIX + "；现在优先用对应平台块「敏感词修复」页签",
     "input_type": "prompt", "sort_order": 93},

    # ---------------- 敏感词校验 ----------------
    # 词库是宽口径过滤库（广告库里有「网络/客服」，涉枪涉爆里有「解放军」），
    # 所以按文件分级：block = 命中即不合格；warn = 只展示不拦；url = 只扫链接。
    {"key": "sensitive.enabled", "value": "1", "category": "sensitive", "label": "启用敏感词校验",
     "hint": "润色完成后先校验，不合格就带着命中词清单让模型再润色，合格才排版/交付",
     "input_type": "select", "options": _YES_NO, "sort_order": 10},
    {"key": "sensitive.lexicon_dir", "value": "", "category": "sensitive", "label": "词库目录",
     "hint": "Sensitive-lexicon 仓库根目录（词库在 Vocabulary/*.txt）；留空用内置位置 "
             "app/project/Sensitive-lexicon-main",
     "input_type": "text", "prober": "dir", "sort_order": 20},
    {"key": "sensitive.tiers", "value": "block,warn,url", "category": "sensitive",
     "label": "启用的词库分级", "hint": "block=拦截（色情/暴恐/涉枪涉爆/反动/政治）；"
             "warn=只提示（民生/贪腐/补充/其他/疫情）；url=非法网址（只扫正文链接）；"
             "off=备用大库（广告/GFW/网易/Tencent，正常词太多，默认不启用）。逗号分隔，"
             "要启用大库就加上 off",
     "input_type": "text", "sort_order": 30},
    {"key": "sensitive.min_len", "value": "2", "category": "sensitive",
     "label": "最小词长（提示档）", "hint": "提示档短于该长度的词条忽略，防止「网络」这类两字词刷屏；"
             "拦截档保留单字脏字，避免漏放",
     "input_type": "number", "sort_order": 40},
    {"key": "sensitive.max_rounds", "value": "3", "category": "sensitive",
     "label": "最大修复轮数", "hint": "命中后每轮把「命中词 + 稿件」交回模型重写，超过轮数仍未通过 → "
             "判不合格、不交付；每轮命中数没有下降会提前停止，避免烧钱",
     "input_type": "number", "sort_order": 50},
    {"key": "sensitive.political_rounds", "value": "1", "category": "sensitive",
     "label": "涉政命中最多修复几轮",
     "hint": "命中政治类（机构名 / 党派 / 人名）时单独限轮：实测这类词模型改不动"
             "（「中共中央办公厅」改不了），跑满 3 轮只是白花钱。默认 1 轮，之后转人工",
     "input_type": "number", "sort_order": 51},
    {"key": "sensitive.auto_fix", "value": "1", "category": "sensitive", "label": "自动二次润色",
     "hint": "关闭则命中即判不合格，只展示命中词，由人工改写后重新跑",
     "input_type": "select", "options": _YES_NO, "sort_order": 60},
    {"key": "sensitive.check_title", "value": "1", "category": "sensitive", "label": "标题也校验",
     "hint": "标题命中同样要修复（先尝试重新生成标题，仍命中再直接改写标题）",
     "input_type": "select", "options": _YES_NO, "sort_order": 70},
    {"key": "sensitive.check_url", "value": "1", "category": "sensitive", "label": "扫描正文里的链接",
     "hint": "用「非法网址」词库匹配正文中的 URL / 域名，命中按拦截处理",
     "input_type": "select", "options": _YES_NO, "sort_order": 80},
    {"key": "sensitive.strict", "value": "0", "category": "sensitive", "label": "严格模式（提示档也拦）",
     "hint": "开启后 warn 档（民生/贪腐等）也算不合格。默认关闭：这些词在正常社会新闻里很常见，"
             "全拦会导致反复改写",
     "input_type": "select", "options": _YES_NO, "sort_order": 90},
    {"key": "sensitive.block_export", "value": "1", "category": "sensitive",
     "label": "未通过则不交付", "hint": "关闭则只记录命中词，仍然导出到 output_finish（不建议）",
     "input_type": "select", "options": _YES_NO, "sort_order": 100},
    {"key": "sensitive.default_whitelist", "value": "1", "category": "sensitive",
     "label": "启用内置白名单", "hint": "内置剔除词库里的正常词：政府 / 法院 / 中央 / 北京 / 武汉 等"
             "（实测这些词会让每篇政务、司法类稿件白跑一轮修复）。关闭则完全按词库原样校验",
     "input_type": "select", "options": _YES_NO, "sort_order": 105},
    {"key": "sensitive.whitelist", "value": "", "category": "sensitive", "label": "白名单",
     "hint": "一行一个词。词条级：整条忽略；片段级：命中前后 30 字内出现白名单词时降级为提示。"
             "适合放地名、机构名、案件常用中性表述",
     "input_type": "textarea", "sort_order": 110},
    {"key": "sensitive.extra_words", "value": "", "category": "sensitive", "label": "追加拦截词",
     "hint": "一行一个词，一律按「拦截」处理（词库没覆盖的平台禁忌词写这里）",
     "input_type": "textarea", "sort_order": 120},

    # ---------------- 发布分发 ----------------
    # 百家号动态：平台没有可用的保存接口，暂不做自动发布，只交付成稿（见 style.FORMS）
    {"key": "publish.baijia_video", "value": "1", "category": "publish",
     "label": "百家号图文带视频",
     "hint": "开启后：视频先传到百家号视频库，再以平台视频块插进草稿正文（成片保留原音轨；"
             "源片自身没音轨时上传前自动补一条静音轨，否则转码报 NO_AUDIO）；"
             "关闭则只带图片，视频留交付目录手动插",
     "input_type": "select", "options": _YES_NO, "sort_order": 10},

    # ---------------- 文章排版 ----------------
    {"key": "layout.auto_count", "value": "1", "category": "layout", "label": "素材数量按文章长短自动决定",
     "hint": "开启后：短文（≤600 字或段落少）1 张图；中等 2~3 张；长文（1500 字以上/段落多）4~5 张并配 1 个视频",
     "input_type": "select", "options": _YES_NO, "sort_order": 5},
    {"key": "layout.image_cap", "value": "6", "category": "layout", "label": "自动模式下图片上限",
     "hint": "长文最多插几张图（素材不够时按实际取）", "input_type": "number", "sort_order": 6},
    {"key": "layout.video_cap", "value": "1", "category": "layout", "label": "自动模式下视频上限",
     "hint": "视频只做配片，建议 1 个", "input_type": "number", "sort_order": 7},
    {"key": "layout.allow_frames", "value": "1", "category": "layout", "label": "视频抽帧参与排版",
     "hint": "开启后抽帧与正式图片同池按相关度排序（画面比搜索图更贴题时会被自动选上）；"
             "关闭则只在图片不够时兜底。排版页始终可以手动插入抽帧",
     "input_type": "select", "options": _YES_NO, "sort_order": 8},
    {"key": "layout.frames_per_video", "value": "2", "category": "layout", "label": "同一视频最多取几张抽帧",
     "hint": "同一段片子的帧画面高度相似，连续放多张会显得凑数；名额没取满时会自动回补",
     "input_type": "number", "sort_order": 9},
    {"key": "layout.frame_slots", "value": "1", "category": "layout", "label": "自动排版至少用几张抽帧",
     "hint": "有视频抽帧时预留几张图的位置给它（0 = 只按相关度选，不保证用上）；"
             "短文只插 1 张图时不强留，优先用最相关的正式图片",
     "input_type": "number", "sort_order": 10},
    {"key": "layout.max_images", "value": "2", "category": "layout", "label": "固定插入图片数量（手动模式）",
     "hint": "关闭「按文章长短自动决定」后才生效；只取相关度最高的前 N 张",
     "input_type": "number", "sort_order": 10},
    {"key": "layout.max_videos", "value": "1", "category": "layout", "label": "固定插入视频数量（手动模式）",
     "hint": "关闭自动模式后才生效", "input_type": "number", "sort_order": 20},
    {"key": "layout.micro_image_cap", "value": "3", "category": "layout", "label": "短稿配图上限（微头条 / 百家动态）",
     "hint": "短稿版面短，自动排版最多插几张图（0 = 不配图）；人工仍可在排版页增删",
     "input_type": "number", "sort_order": 11},
    {"key": "layout.min_gap", "value": "1", "category": "layout", "label": "素材之间至少隔几段",
     "hint": "避免连续两张图挤在一起；长文图多时会自动按可用段落摊开",
     "input_type": "number", "sort_order": 30},
    {"key": "layout.detect_inline_title", "value": "1", "category": "layout", "label": "识别正文中的小标题",
     "hint": "识别加粗独占行、短行等疑似小标题；误判过多时会自动回退",
     "input_type": "select", "options": _YES_NO, "sort_order": 40},
    {"key": "layout.image_caption", "value": "1", "category": "layout", "label": "图片下方输出图注",
     "hint": "用素材标题或检索词作为图注", "input_type": "select", "options": _YES_NO, "sort_order": 50},
    {"key": "layout.image_ref", "value": "path", "category": "layout", "label": "图片引用方式",
     "hint": "本地路径便于预览和上传；外链适合直接粘贴到编辑器",
     "input_type": "select", "sort_order": 60,
     "options": [{"value": "path", "label": "本地绝对路径"}, {"value": "url", "label": "可访问 URL"}]},
    {"key": "layout.copy_image_mode", "value": "inline", "category": "layout",
     "label": "一键复制的图片方式",
     "hint": "内联：图片压缩后随剪贴板一起带走，粘贴时由平台编辑器自己上传（不依赖网络与账号）；"
             "平台图床：先把图传到头条/百家图床再复制，最稳但要账号已登录、每张多等 1~2 秒；"
             "纯文字：不复制图片，只留一行配图占位。排版页下拉可临时切换",
     "input_type": "select", "sort_order": 70,
     "options": [{"value": "inline", "label": "内联（推荐）"},
                 {"value": "upload", "label": "先传平台图床"},
                 {"value": "text", "label": "纯文字"}]},
    {"key": "layout.copy_image_max_width", "value": "1080", "category": "layout",
     "label": "复制时图片最大宽度", "hint": "内联图片会等比缩到这个宽度（480~2400）",
     "input_type": "number", "sort_order": 80},
    {"key": "layout.copy_image_quality", "value": "82", "category": "layout",
     "label": "复制时图片 JPEG 质量", "hint": "40~95；越小粘贴越快，平台二次压缩后越容易糊",
     "input_type": "number", "sort_order": 90},

    # ---------------- 来源标记（只用于归一/筛选，不影响产出与发布） ----------------
    {"key": "style.tt_aliases", "value": "头条,今日头条,头条号,toutiao,tt.",
     "category": "style", "label": "头条来源别名",
     "hint": "表格「来源」列里出现这些词就标记为头条来源（逗号分隔，不区分大小写）；"
             "只是打标记做筛选统计，不影响要产出/发布哪些平台",
     "input_type": "text", "sort_order": 20},
    {"key": "style.bjh_aliases", "value": "百家,百家号,baijia,bjh,百度,baidu",
     "category": "style", "label": "百家来源别名",
     "hint": "同上；先判百家再判头条，避免 baijiahao.baidu.com 被误标成头条",
     "input_type": "text", "sort_order": 30},
]

#: 分块提示词的输入说明（避免标题/正文/润色配错）
PROMPT_STAGE_INPUT = {
    "title": "输入 = 原文全文",
    "body": "输入 = 原文全文",
    "polish": "输入 = 上一轮初稿（不是原文）",
    "fix": "输入 = 润色稿 + 命中词清单（{{hits}} 由程序生成）",
}

_PROMPT_PRESETS: List[Dict[str, Any]] = []
for _bi, _block in enumerate(PROMPT_BLOCKS):
    for _si, _stage in enumerate(_block["stages"]):
        _PROMPT_PRESETS.append({
            "key": f"prompt.{_block['platform']}.{_block['form']}.{_stage}",
            "value": _block["stages"][_stage],
            "category": "prompt",
            "label": f"{_block['title']} · {PROMPT_STAGE_LABEL[_stage]}",
            "hint": PROMPT_STAGE_INPUT[_stage] + "；必须带 {{article_text}}，缺失时程序会把正文追加到末尾",
            "input_type": "prompt",
            "sort_order": 10 + _bi * 10 + _si,
        })
PRESETS += _PROMPT_PRESETS

_PRESET_KEYS = {p["key"] for p in PRESETS}

# 已废弃的旧配置项：启动时清掉，避免设置页出现没人认的僵尸项
DEPRECATED_KEYS = ["rewrite.article_length", "rewrite.enable_title",
                   "publish.baijia_dynamic_api",
                   "style.rule_mode"]        # 同源错开规则已取消：两平台都产出

# 预置项默认值变更的一次性迁移：(key, 旧默认值, 新默认值)。
# 只改「还停留在旧默认值」的项 —— 用户自己调过的数字一律不动。
# micro_max_chars 300 → 400：短文阈值统一，且百家号动态也用同一阈值。
PRESET_MIGRATIONS: List[tuple] = [
    ("rewrite.micro_max_chars", "300", "400"),
    # 降重提示词改为「内置基础 + 可选附加要求」：老库里存着初版完整模板，改回空
    ("rewrite.prompt_dedup", PROMPT_DEDUP_V1, ""),
    # 查重口径从「短语级（10 字）」调到「整句级（13 字）」，阈值同步放宽到 20%
    ("rewrite.dedup_minrun", "10", "13"),
    ("rewrite.dedup_ratio", "0.18", "0.20"),
]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 种子 / 读取
def seed_defaults() -> int:
    """插入缺失的预置项，已存在的键不覆盖用户改过的值。"""
    now = _now()
    added = 0
    with write_conn() as conn:
        conn.execute(
            f"DELETE FROM app_settings WHERE key IN ({','.join('?' * len(DEPRECATED_KEYS))})",
            DEPRECATED_KEYS,
        )
        for p in PRESETS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value, category, label, hint, input_type,"
                " options, prober, is_secret, sort_order, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [p["key"], str(p.get("value", "")), p["category"], p["label"], p.get("hint", ""),
                 p.get("input_type", "text"), json.dumps(p.get("options", []), ensure_ascii=False),
                 p.get("prober", ""), 1 if p.get("is_secret") else 0, int(p.get("sort_order", 0)), now],
            )
            added += cur.rowcount or 0
            # 已存在的键：同步展示相关的元数据，但绝不覆盖用户改过的 value
            conn.execute(
                "UPDATE app_settings SET category=?, label=?, hint=?, input_type=?, options=?,"
                " prober=?, sort_order=? WHERE key=?",
                [p["category"], p["label"], p.get("hint", ""), p.get("input_type", "text"),
                 json.dumps(p.get("options", []), ensure_ascii=False), p.get("prober", ""),
                 int(p.get("sort_order", 0)), p["key"]],
            )
        # 默认值本身改了（不是用户改的）：只迁移还等于旧默认值的那部分
        for key, old, new in PRESET_MIGRATIONS:
            conn.execute(
                "UPDATE app_settings SET value=?, updated_at=? WHERE key=? AND value=?",
                [new, now, key, old],
            )
    return added


def list_settings(category: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT * FROM app_settings"
    args: List[Any] = []
    if category:
        sql += " WHERE category=?"
        args.append(category)
    sql += " ORDER BY category, sort_order, key"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    for r in rows:
        r["options"] = _loads(r.get("options"))
        if r.get("is_secret"):
            r["value"] = mask_secret(r.get("value") or "")
    return rows


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row and row["value"] is not None else default) or default


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get_setting(key).strip() or default))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(key).strip() or default)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = get_setting(key).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def all_settings() -> Dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r["key"]: r["value"] or "" for r in rows}


def save_settings(items: Dict[str, Any]) -> int:
    """批量保存。只接受预置过的键；密钥项空值视为「不修改」。"""
    if not isinstance(items, dict):
        return 0
    with get_conn() as conn:
        rows = conn.execute("SELECT key, is_secret FROM app_settings").fetchall()
        known = {r["key"]: r["is_secret"] for r in rows}
    unknown = [k for k in items if k not in known]
    if unknown:
        raise KeyError("未知的配置项：" + ", ".join(unknown))
    now = _now()
    saved = 0
    with write_conn() as conn:
        for key, value in items.items():
            text = "" if value is None else str(value)
            if known.get(key) and not text.strip():
                continue  # 密钥留空 = 保持原值
            conn.execute("UPDATE app_settings SET value=?, updated_at=? WHERE key=?", [text, now, key])
            saved += 1
    return saved


def preset_value(key: str) -> str:
    """取某个配置项的出厂默认值。"""
    for p in PRESETS:
        if p["key"] == key:
            return str(p.get("value", ""))
    return ""


def reset_settings(keys: Optional[List[str]] = None) -> int:
    """把指定配置项（缺省全部）恢复为预置默认值。"""
    targets = [k for k in (keys or []) if k in _PRESET_KEYS] or sorted(_PRESET_KEYS)
    preset = {p["key"]: preset_value(p["key"]) for p in PRESETS}
    now = _now()
    with write_conn() as conn:
        for k in targets:
            conn.execute("UPDATE app_settings SET value=?, updated_at=? WHERE key=?",
                         [preset.get(k, ""), now, k])
    return len(targets)


# ---------------------------------------------------------------- 可执行文件 / 路径
def resolve_binary(key: str, *fallback_names: str) -> str:
    """按「页面配置 → 系统 PATH → 命令名」的顺序解析可执行文件。"""
    configured = (get_setting(key) or "").strip()
    if configured:
        return configured
    for name in fallback_names:
        found = shutil.which(name)
        if found:
            return found
    return fallback_names[0] if fallback_names else ""


def probe_path(value: str, prober: str = "bin") -> Dict[str, Any]:
    """页面「探测」按钮：检查路径是否存在（bin 还会试跑 --version）。"""
    raw = (value or "").strip()
    if not raw:
        return {"ok": False, "message": "未配置，运行时将回退到系统 PATH 自动探测", "value": ""}
    resolved = raw
    if not Path(raw).expanduser().exists():
        found = shutil.which(raw)
        if found:
            resolved = found
    p = Path(resolved).expanduser()
    if prober == "dir":
        ok = p.is_dir()
        return {"ok": ok, "message": (f"目录可用：{p}" if ok else f"目录不存在：{p}"), "value": str(p)}
    if not p.is_file():
        return {"ok": False, "message": f"找不到文件：{raw}（留空则自动探测）", "value": raw}
    if prober != "bin":
        return {"ok": True, "message": f"文件存在：{p}", "value": str(p)}
    try:
        out = subprocess.run([str(p), "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        first = (out.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        msg = first[0] if first else f"文件存在：{p}"
    except Exception as exc:  # noqa: BLE001
        msg = f"文件存在但无法执行（{exc.__class__.__name__}）：{p}"
    return {"ok": True, "message": msg, "value": str(p)}


# ---------------------------------------------------------------- 内部工具
def mask_secret(value: str) -> str:
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 10:
        return "*" * len(s)
    return f"{s[:6]}***{s[-4:]}"


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []


def expand_path(value: str) -> str:
    p = Path(os.path.expandvars(str(value or ""))).expanduser()
    return str(p)


# ---------------------------------------------------------------- 提示词渲染
ARTICLE_VAR = "{{article_text}}"


def _render(tpl: str, article_text: str, **extra: str) -> str:
    """模板渲染：替换 {{变量}}；模板没写正文占位符时把正文追加到末尾。"""
    if not str(tpl or "").strip():
        return article_text
    out = str(tpl)
    for name, value in dict(article_text=article_text, **extra).items():
        out = out.replace("{{" + name + "}}", value or "")
    if ARTICLE_VAR not in str(tpl):
        out = out.rstrip() + "\n\n" + article_text
    return out


def render_prompt(key: str, article_text: str, **extra: str) -> str:
    """把提示词模板渲染成最终提示词（按 key 精确取）。"""
    return _render(get_setting(key), article_text, **extra)


def render_template(tpl: str, article_text: str, **extra: str) -> str:
    """用调用方给的模板渲染（不查库），供程序内部拼接的提示词使用。"""
    return _render(tpl, article_text, **extra)


def prompt_template_for(platform: str, form: str, stage: str) -> str:
    """取某个平台 · 形态 · 阶段的提示词模板：分块模板优先，旧通用模板兜底。"""
    key = f"prompt.{platform}.{form}.{stage}"
    tpl = (get_setting(key) or "").strip()
    if tpl:
        return tpl
    return (get_setting(PROMPT_FALLBACK.get(stage, "")) or "").strip()


def render_prompt_for(platform: str, form: str, stage: str,
                      article_text: str, **extra: str) -> str:
    """按平台 · 形态 · 阶段渲染提示词（改写引擎统一走这里）。"""
    return _render(prompt_template_for(platform, form, stage), article_text, **extra)


def prompt_groups() -> List[Dict[str, Any]]:
    """提示词面板的数据源：平台块 → 阶段页签，值取当前设置。

    以后接新平台只需要在 PROMPT_BLOCKS 加一块，前端按这个结构渲染，
    不会因为平台变多把页面撑成一长条平铺文本框。
    """
    values = all_settings()
    out: List[Dict[str, Any]] = []
    for block in PROMPT_BLOCKS:
        stages = []
        for stage, fallback in block["stages"].items():
            key = f"prompt.{block['platform']}.{block['form']}.{stage}"
            stages.append({
                "stage": stage,
                "label": PROMPT_STAGE_LABEL.get(stage, stage),
                "key": key,
                "value": values.get(key, fallback),
                "default": preset_value(key),
                "hint": PROMPT_STAGE_INPUT.get(stage, ""),
            })
        out.append({
            "platform": block["platform"],
            "form": block["form"],
            "title": block["title"],
            "hint": block.get("hint", ""),
            "stages": stages,
        })
    return out
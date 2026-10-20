"""
trend_watch.py - 新词监控脚本，用于发现刚冒头的搜索新词。

用法:
    python3 trend_watch.py

功能:
    1. GitHub breakout（发现源）：抓取创建不足 14 天、日均涨星 200+ 或总星 400+ 的新仓库，
       仓库名即产品/技术新词。
    2. HuggingFace 新模型（发现源）：只用 trendingScore 拉候选，只保留 created_at 在 7 天内的
       模型，一个模型一条结果（term=模型名）；媒体生成类 + API 可用 + 许可证未禁商用标 new_model。
    3. Reddit rising（发现源）：抓取 r/all/rising 帖子标题，提取 2-4 词关键词短语。
    4. X (Twitter) 趋势（发现源）：优先尝试 Nitter 公开实例与社交搜索降级抓取过去 8 小时高互动推文，提取产品名和技术名词；失败时静默跳过。
    5. Google Trends（仅验证源，不做发现）：14 天命中率核验时用 Trends 查词的持续热度；
       注意丢弃最近 2 天未结算数据，避免 0→100 假峰值误判。
    6. 过滤与分类：过滤纯人名八卦与无明确意图词，保留工具型（tool）或资讯型（info）意图的词。
    7. 命中率追踪：将新发现词写入 hidden_files/word_tracking.json；通过 check_hit_rate() 对 14 天前的词用 Google Trends 核验热度翻倍情况并输出报告。
    8. Jev 打分层：用 TypeSafe Jev 结构化决策模型给候选词打三维分（是否产品/技术名、商业意图、
       新词价值），通道由 JEV_PROVIDER 切换（openrouter 默认，无 key 回退 OpenCode Zen 免费通道），失败不阻塞。
    9. 变现分流：Jev 后按 classify_monetization 把候选分到 quick_paid_site / tool_page /
       content / watch（generic 低分直接 reject），排序时路线优先。
    9. 输出：生成 ~/workspace/trend-monitor/outputs/YYYY-MM-DD-HH.json（北京时间命名）。
"""

import os
import shutil
import sys
import re
import time
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from collections import Counter

# 解决本机 no_proxy 环境变量中方括号 IPv6 条目导致 httpx / requests 解析崩溃的问题
os.environ['no_proxy'] = 'localhost,127.0.0.1,::1'
os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1'

import requests
import pandas as pd
from pytrends.request import TrendReq
from huggingface_hub import HfApi

# ---------------------------------------------------------------------------
# Monkey-patch pytrends TrendReq 以适应 Google Trends 最新的 API 变动
# 1. 自动注入 cookies（避免 429 / 403）
# 2. _tokens 请求改用 GET 方法（避免 Google 报 411 Length Required）
# ---------------------------------------------------------------------------
def _patch_pytrends():
    orig_tokens = TrendReq._tokens

    def _patched_tokens(self):
        if not getattr(self, 'cookies', None):
            s = requests.Session()
            s.headers.update(self.headers)
            try:
                r = s.get('https://trends.google.com/?geo=US', timeout=10)
                self.cookies = r.cookies
            except Exception:
                pass
        widget_dicts = self._get_data(
            url=TrendReq.GENERAL_URL,
            method=TrendReq.GET_METHOD,
            params=self.token_payload,
            trim_chars=4,
        ).get('widgets', [])
        
        first_region_token = True
        self.related_queries_widget_list[:] = []
        self.related_topics_widget_list[:] = []
        for widget in widget_dicts:
            if widget.get('id') == 'TIMESERIES':
                self.interest_over_time_widget = widget
            if widget.get('id') == 'GEO_MAP' and first_region_token:
                self.interest_by_region_widget = widget
                first_region_token = False
            if 'RELATED_TOPICS' in widget.get('id', ''):
                self.related_topics_widget_list.append(widget)
            if 'RELATED_QUERIES' in widget.get('id', ''):
                self.related_queries_widget_list.append(widget)

    TrendReq._tokens = _patched_tokens

_patch_pytrends()


# ---------------------------------------------------------------------------
# 关键词分类与过滤规则
# ---------------------------------------------------------------------------

STOP_WORDS = set([
    'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', "aren't", 'as', 'at',
    'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by', 'can', "can't", 'cannot',
    'could', "couldn't", 'did', "didn't", 'do', 'does', "doesn't", 'doing', "don't", 'down', 'during', 'each',
    'few', 'for', 'from', 'further', 'had', "hadn't", 'has', "hasn't", 'have', "haven't", 'having', 'he', "he'd",
    "he'll", "he's", 'her', 'here', "here's", 'hers', 'herself', 'him', 'himself', 'his', 'how', "how's", 'i',
    "i'd", "i'll", "i'm", "i've", 'if', 'in', 'into', 'is', "isn't", 'it', "it's", 'its', 'itself', "let's",
    'me', 'more', 'most', "mustn't", 'my', 'myself', 'no', 'nor', 'not', 'of', 'off', 'on', 'once', 'only', 'or',
    'other', 'ought', 'our', 'ours', 'ourselves', 'out', 'over', 'own', 'same', "shan't", 'she', "she'd", "she'll",
    "she's", 'should', "shouldn't", 'so', 'some', 'such', 'than', 'that', "that's", 'the', 'their', 'theirs', 'them',
    'themselves', 'then', 'there', "there's", 'these', 'they', "they'd", "they'll", "they're", "they've", 'this',
    'those', 'through', 'to', 'too', 'under', 'until', 'up', 'very', 'was', "wasn't", 'we', "we'd", "we'll", "we're",
    "we've", 'were', "weren't", 'what', "what's", 'whatever', 'when', "when's", 'where', "where's", 'which',
    'while', 'who', "who's", 'whom', 'why', "why's", 'with', "won't", 'would', "wouldn't", 'you', "you'd", "you'll",
    "you're", "you've", 'your', 'yours', 'yourself', 'yourselves', 'just', 'like', 'get', 'got', 'one', 'new', 'first',
    'also', 'even', 'make', 'made', 'see', 'saw', 'look', 'looks', 'looking', 'come', 'came', 'go', 'went', 'going'
])

# 纯人名八卦 / 明星恋情特征词
GOSSIP_PATTERNS = re.compile(
    r'\b(dating|boyfriend|girlfriend|affair|breakup|broken up|divorce|divorcing|'
    r'cheating|rumors|spotted together|kissing|secret romance|ex-husband|ex-wife|'
    r'scandal|hooking up|baby bump|engaged|engagement ring|paparazzi|flirting)\b',
    re.I
)

# 无搜索意图特征词（感叹词、网络垃圾词）
JUNK_PATTERNS = re.compile(
    r'^(wow|omg|lol|lmao|wtf|damn|oops|yay|yikes|meh|nah|huh|smh|bruh|idc|idk|\d+|[a-zA-Z])$',
    re.I
)

# Reddit 纯寒暄/无意义短语黑名单
USELESS_PHRASES = set([
    # 问候与寒暄
    'good morning', 'good afternoon', 'good evening', 'good night',
    'good day', 'have a good', 'have a great', 'hope you have',
    'thank you', 'thanks you', 'thanks a lot', 'thank u', 'thanks for',
    'thanks so much', 'thank you all', 'thank you so much', 'thanks in advance',
    'no problem', 'you are welcome', "you're welcome", 'you welcome',
    'hello everyone', 'hey guys', 'hi all', 'hi guys',
    
    # 评价/感叹/主观短语
    'really good', 'pretty good', 'looks good', 'sounds good', 'feels good',
    'so good', 'very good', 'too good', 'not bad', 'pretty cool', 'really cool',
    'super cool', 'so cool', 'really nice', 'pretty nice', 'so nice',
    'really bad', 'pretty bad', 'too bad',
    'holy shit', 'oh my god', 'what the fuck', 'what the hell',
    
    # 祝贺与节日
    'happy birthday', 'happy new year', 'happy anniversary',
    'merry christmas', 'happy holidays', 'congratulations to',
    
    # 提问与互动短语
    'anyone else', 'any thoughts', 'any idea', 'any ideas', 'any advice',
    'any suggestions', 'any recommendations', 'what do you think',
    'what do you', 'what are your', 'how do you', 'what is this',
    'does anyone', 'has anyone', 'can someone', 'could someone',
    'let me know', 'let us know', 'tell me', 'wondering if',
    
    # 表达不确定/个人感受
    'feel like', 'feels like', 'looks like', 'sounds like', 'seems like',
    'not sure', 'dont know', "don't know", 'no idea', 'have no idea',
    'i think', 'i feel', 'i hope', 'i wish', 'i guess', 'i believe',
    'just wanted', 'wanted to know', 'wanted to share', 'wanted to ask',
    
    # 求助泛词
    'need help', 'need advice', 'please help', 'help me', 'help needed',
    'some help', 'looking for help',
    
    # 时间/泛指结构
    'first time', 'last time', 'next time', 'long time', 'same time',
    'years ago', 'days ago', 'hours ago', 'months ago', 'weeks ago',
    'every day', 'every single', 'all the time', 'one of the',
    'part of the', 'kind of', 'sort of', 'as well as', 'due to',
    'looking for', 'check this', 'check out', 'take a look',
    'let us go', 'lets go', "let's go", 'here we go', 'come on'
])

# 常见口语/寒暄/泛词正则（无明确搜索意图）
CONVERSATIONAL_PATTERNS = re.compile(
    r'^(good (morning|night|afternoon|evening|day)|'
    r'thank(s)? (you|u|a lot|so much|in advance)|'
    r'(really|pretty|looks|sounds|feels|so|very|too) (good|bad|cool|nice|great)|'
    r'happy (birthday|new year|anniversary|holidays)|'
    r'anyone else|any (thoughts|idea|ideas|advice|suggestions|recommendations)|'
    r'(feel|looks|sounds|seems) like|'
    r'(not sure|dont know|don\'t know|no idea|have no idea)|'
    r'let (me|us) know|need help|please help|'
    r'looking (for|forward)|check (this|out)|take a look|'
    r'(first|last|next|long|same) time|'
    r'what do you (think|mean|do)|'
    r'(holy shit|oh my god|what the (fuck|hell)))$',
    re.I
)

# 人名正则：名+姓结构（两段首字母大写的英文单词，如 "Kalen DeBoer", "Jon Husted", "Bob Chesney"）
PERSON_PATTERN = re.compile(r"^[A-Z][a-zA-Z'\-]+\s+[A-Z][a-zA-Z'\-]+$")

# 常见英文名字库（辅助判断人名）
COMMON_FIRST_NAMES = set([
    # 男名与常见昵称
    'james', 'john', 'jon', 'robert', 'bob', 'bobby', 'michael', 'mike', 'william', 'bill', 'billy',
    'david', 'dave', 'richard', 'rick', 'dick', 'charles', 'charlie', 'chuck', 'joseph', 'joe',
    'thomas', 'tom', 'tommy', 'christopher', 'chris', 'daniel', 'dan', 'danny', 'matthew', 'matt',
    'anthony', 'tony', 'donald', 'don', 'mark', 'paul', 'steven', 'steve', 'andrew', 'andy',
    'kenneth', 'ken', 'joshua', 'josh', 'kevin', 'brian', 'bryan', 'george', 'edward', 'ed',
    'ronald', 'ron', 'timothy', 'tim', 'jason', 'jeffrey', 'jeff', 'ryan', 'jacob', 'jake',
    'gary', 'nicholas', 'nick', 'eric', 'erik', 'jonathan', 'stephen', 'larry', 'justin', 'scott',
    'brandon', 'benjamin', 'ben', 'samuel', 'sam', 'gregory', 'greg', 'alexander', 'alex', 'frank',
    'patrick', 'pat', 'raymond', 'ray', 'jack', 'dennis', 'jerry', 'tyler', 'aaron', 'jose',
    'adam', 'nathan', 'henry', 'douglas', 'doug', 'zachary', 'zach', 'peter', 'pete', 'kyle',
    'walter', 'walt', 'ethan', 'jeremy', 'harold', 'keith', 'christian', 'roger', 'noah', 'gerald',
    'carl', 'terry', 'sean', 'shawn', 'austin', 'arthur', 'lawrence', 'jesse', 'dylan', 'albert',
    'willie', 'gabriel', 'logan', 'alan', 'allen', 'juan', 'wayne', 'roy', 'ralph', 'randy',
    'eugene', 'vincent', 'russell', 'louis', 'bruce', 'curt', 'curtis', 'kalen', 'caleb', 'khalil',
    'todd', 'trevor', 'travis', 'cody', 'colin', 'collin', 'cameron', 'connor', 'conor', 'cole',
    'chase', 'blake', 'brock', 'grant', 'spencer', 'derek', 'dustin', 'shane', 'mitchell', 'mitch',
    'mason', 'morgan', 'hunter', 'cooper', 'carter', 'landon', 'lucas', 'luke', 'owen', 'liam',
    'elias', 'eli', 'oliver', 'theodore', 'theo', 'leo', 'max', 'chet', 'deion', 'lane', 'kirby',
    'dabo', 'cade', 'quinn', 'arch', 'jaxson', 'shedeur', 'cam', 'carson', 'jalen', 'bo', 'dillon',
    'will', 'garrett', 'drew', 'brad', 'brett', 'troy', 'trent', 'dalton', 'damian', 'dominic',
    'ian', 'jasper', 'jonah', 'jude', 'kai', 'miles', 'nolan', 'silas', 'tristan', 'victor', 'xavier',
    'nico', 'nicolas', 'dante', 'malik', 'kobe', 'lebron', 'shaq', 'giannis', 'luka', 'novak',
    'rafael', 'carlos', 'lionel', 'cristiano', 'kylian', 'erling', 'lamine', 'ashton', 'jalen',
    # 女名与常见昵称
    'mary', 'patricia', 'patty', 'jennifer', 'jen', 'jenny', 'linda', 'elizabeth', 'liz', 'beth',
    'barbara', 'susan', 'sue', 'jessica', 'sarah', 'sara', 'karen', 'lisa', 'nancy', 'betty',
    'margaret', 'maggie', 'sandra', 'sandy', 'ashley', 'kimberly', 'kim', 'emily', 'donna', 'michelle',
    'carol', 'amanda', 'melissa', 'deborah', 'deb', 'stephanie', 'steph', 'rebecca', 'becky',
    'sharon', 'laura', 'cynthia', 'cindy', 'kathleen', 'kathy', 'amy', 'shirley', 'angela', 'angie',
    'helen', 'anna', 'brenda', 'pamela', 'pam', 'nicole', 'emma', 'samantha', 'sam', 'katherine',
    'kate', 'katie', 'christine', 'chris', 'debra', 'rachel', 'catherine', 'carolyn', 'janet', 'ruth',
    'maria', 'heather', 'diane', 'virginia', 'julie', 'joyce', 'victoria', 'vicky', 'olivia', 'kelly',
    'christina', 'tina', 'lauren', 'joan', 'evelyn', 'judith', 'judy', 'megan', 'cheryl', 'andrea',
    'hannah', 'martha', 'jacqueline', 'jackie', 'frances', 'gloria', 'ann', 'anne', 'teresa', 'kathryn',
    'janice', 'jean', 'alice', 'madison', 'doris', 'abigail', 'abby', 'julia', 'grace', 'denise',
    'amber', 'marilyn', 'beverly', 'danielle', 'theresa', 'sophia', 'marie', 'diana', 'brittany',
    'natalie', 'isabella', 'charlotte', 'rose', 'kayla', 'alexis', 'taylor', 'alyssa', 'brianna',
    'chloe', 'zoe', 'hailey', 'mia', 'claire', 'elena'
])

COMMON_LAST_NAMES = set([
    'smith', 'johnson', 'williams', 'brown', 'jones', 'garcia', 'miller', 'davis', 'rodriguez', 'martinez',
    'hernandez', 'lopez', 'gonzalez', 'wilson', 'anderson', 'thomas', 'taylor', 'moore', 'jackson', 'martin',
    'lee', 'perez', 'thompson', 'white', 'harris', 'sanchez', 'clark', 'ramirez', 'lewis', 'robinson',
    'walker', 'young', 'allen', 'king', 'wright', 'scott', 'torres', 'nguyen', 'hill', 'flores',
    'green', 'adams', 'nelson', 'baker', 'hall', 'rivera', 'campbell', 'mitchell', 'carter', 'roberts',
    'gomez', 'phillips', 'evans', 'turner', 'diaz', 'parker', 'cruz', 'edwards', 'collins', 'reyes',
    'stewart', 'morris', 'morales', 'murphy', 'cook', 'rogers', 'gutierrez', 'ortiz', 'morgan', 'cooper',
    'peterson', 'bailey', 'reed', 'kelly', 'howard', 'ramos', 'kim', 'cox', 'ward', 'richardson',
    'watson', 'brooks', 'chavez', 'wood', 'james', 'bennett', 'gray', 'mendoza', 'ruiz', 'hughes',
    'price', 'alvarez', 'castillo', 'sanders', 'patel', 'myers', 'long', 'ross', 'foster', 'jimenez',
    'powell', 'jenkins', 'perry', 'russell', 'sullivan', 'bell', 'coleman', 'butler', 'henderson', 'barnes',
    'gonzales', 'fisher', 'vasquez', 'simmons', 'romero', 'jordan', 'patterson', 'alexander', 'hamilton', 'graham',
    'reynolds', 'griffin', 'wallace', 'moreno', 'west', 'cole', 'hayes', 'bryant', 'herrera', 'gibson',
    'chesney', 'narduzzi', 'deboer', 'husted', 'mack', 'cignetti', 'belichick', 'saban', 'smart', 'harbaugh',
    'iamaleava', 'raiola', 'sanders', 'hunter'
])

# 用于防止误把地理/名词判定为人名的词
NON_PERSON_WORDS = set([
    'lake', 'river', 'mountain', 'hill', 'park', 'forest', 'valley', 'island', 'beach',
    'road', 'street', 'avenue', 'boulevard', 'station', 'airport', 'center', 'centre',
    'square', 'building', 'tower', 'bridge', 'stadium', 'arena', 'league', 'cup',
    'football', 'basketball', 'baseball', 'soccer', 'union', 'county', 'district', 'city',
    'state', 'country', 'republic', 'empire', 'game', 'match', 'show', 'series', 'season',
    'episode', 'movie', 'film', 'song', 'album', 'event', 'festival', 'war', 'battle',
    'east', 'west', 'north', 'south', 'central', 'upper', 'lower', 'new', 'old', 'great'
])

# 上下文中出现的人名称谓/职务/角色信号
PERSON_TITLE_PATTERNS = re.compile(
    r'\b(coach|head coach|sen\.|senator|rep\.|representative|gov\.|governor|'
    r'president|dr\.|mr\.|mrs\.|ms\.|quarterback|qb|player|pitcher|striker|goalkeeper|'
    r'actor|actress|singer|starring|rapper|musician|celebrity|author|mayor|'
    r'attorney|prosecutor|judge|spokesman|spokeswoman|he said|she said|'
    r'his career|her career|his season|her season|his contract|her contract|'
    r'dies at|dead at|passes away|passed away|5-star|recruit|prospect|draft|rookie|'
    r'athlete|transfer portal)\b',
    re.I
)

# 明确工具信号词（收紧规则：只有出现明确工具词才标 tool）
EXPLICIT_TOOL_PATTERNS = re.compile(
    r'\b(tool|tools|converter|converters|compressor|compressors|calculator|calculators|'
    r'generator|generators|download|downloads|template|templates|app|apps|software|'
    r'online free|free online|extension|extensions|plugin|plugins|script|scripts|'
    r'cli|sdk|api|editor|editors|solver|solvers|cheatsheet|cheat sheet)\b',
    re.I
)

# 工具/产品/教程类补充信号（用于避免把包含教程/产品的人名技术词误判为纯人名）
TUTORIAL_PRODUCT_PATTERNS = re.compile(
    r'\b(tutorial|guide|course|documentation|docs|platform|library|framework|'
    r'how to use|how to install|how to deploy|how to build|product|release notes)\b',
    re.I
)

# 资讯型（info: 查信息、新闻、赛况、发布日期、规格、规则、怎么看直播等）
INFO_PATTERNS = re.compile(
    r'\b(what is|who is|why is|when is|how much|meaning|definition|review|vs|versus|'
    r'release date|score|schedule|standings|rules|update|policy|recap|news|price|'
    r'history|spec|specs|features|lineup|stats|winner|results|explained|announcement|'
    r'leak|election|football|game|match|highlights|preview|injury|roster|ranking|'
    r'poll|bill|law|stock|earnings|how to watch|how to stream|season|episode|trailer)\b',
    re.I
)


def has_tool_or_product_signal(text: str) -> bool:
    """检查文本中是否包含明确的工具、产品或教程类关键词"""
    return bool(EXPLICIT_TOOL_PATTERNS.search(text) or TUTORIAL_PRODUCT_PATTERNS.search(text))


def is_person_name(term: str, context: str = '') -> bool:
    """加强人名判断：
    term 如果是'名+姓'结构（两个首字母大写的英文单词）且相关事件文本中没有工具/产品/教程类关键词，
    则判定为人名。结合 PERSON_PATTERN 正则、常见名字库及上下文职务辅助判断。
    """
    combined = f"{term} {context}"
    # 若相关事件或搜索词包含明确工具/产品/教程关键词，不判定为纯人名
    if has_tool_or_product_signal(combined):
        return False

    # 八卦/明星恋情特征
    if GOSSIP_PATTERNS.search(combined):
        return True

    clean_term = term.strip()
    words = clean_term.split()

    # 两个单词的英文字符串检查
    if len(words) == 2 and all(re.match(r"^[a-zA-Z'\-]+$", w) for w in words):
        title_term = clean_term.title()
        if PERSON_PATTERN.match(title_term):
            w1 = words[0].lower()
            w2 = words[1].lower()

            # 排除明确的地名或通用名词（如 saraki lake, national park 等）
            if w1 in NON_PERSON_WORDS or w2 in NON_PERSON_WORDS:
                return False

            # 命中常见名字库（名或姓）
            if w1 in COMMON_FIRST_NAMES or w2 in COMMON_LAST_NAMES:
                return True

            # 上下文中出现人名职务/称谓信号（coach, senator, qb, actor 等）
            if PERSON_TITLE_PATTERNS.search(context):
                return True

    # 三词结构支持（如带中间名/教名）
    if len(words) == 3 and all(re.match(r"^[a-zA-Z'\-\.]+$", w) for w in words):
        w1 = words[0].lower()
        w3 = words[2].lower()
        if (w1 in COMMON_FIRST_NAMES or w3 in COMMON_LAST_NAMES) and PERSON_TITLE_PATTERNS.search(context):
            return True

    return False


def classify_intent(term: str, context: str = '') -> str:
    """根据词本身及上下文判断搜索意图 (tool / info / other)
    规则收紧：
    - 绝不将人名标为 tool
    - 只有 term 或相关事件文本中包含明确工具信号词（如：tool, converter, compressor, calculator, generator, download, template, app, software, online free 等）才标 tool
    - 否则若包含资讯信号词则标 info，其余标 other
    """
    # 坚决不将人名标为 tool
    if is_person_name(term, context):
        if INFO_PATTERNS.search(f"{term} {context}"):
            return 'info'
        return 'other'

    combined = f"{term} {context}".lower()

    # 明确工具信号词
    if EXPLICIT_TOOL_PATTERNS.search(combined):
        return 'tool'

    # 资讯型信号词
    if INFO_PATTERNS.search(combined):
        return 'info'

    return 'other'


# ---------------------------------------------------------------------------
# 改动1 & 改动2: 商业价值打分与事件类型标记规则
# ---------------------------------------------------------------------------

HIGH_VALUE_EN = [
    'best', 'vs', 'how to', 'price', 'review', 'alternative', 'comparison',
    'buy', 'deal', 'discount', 'coupon', 'free trial', 'sign up', 'login',
    'tutorial', 'top 10', 'cheapest', 'promo code', 'pricing'
]

HIGH_VALUE_ZH = [
    '价格', '对比', '评测', '测评', '教程', '购买', '优惠', '攻略', '哪个', '怎么样', '值得买'
]

HIGH_VALUE_EN_PATTERNS = [
    re.compile(r'\b' + re.escape(w) + r'\b', re.I) for w in HIGH_VALUE_EN
]

# 事件类型正则：product_launch
PRODUCT_LAUNCH_PATTERN = re.compile(
    r'\b(launch(?:es|ed)?|release(?:d|s)?|announce(?:ments?|s|d)?|unveil(?:ed|s)?|'
    r'debut(?:ed|s)?|rollout|shipping|pre-?order|keynote)\b|'
    r'(发布|推出|上线|上市|首发|新品)',
    re.I
)

# 事件类型正则：policy
POLICY_PATTERN = re.compile(
    r'\b(polic(?:y|ies)?|regulations?|ban(?:ned|s)?|laws?|bills?|'
    r'rule\s+changes?|executive\s+orders?|legislation|tariffs?)\b|'
    r'(禁令|政策|新规|法规|关税|制裁)',
    re.I
)

# 事件类型正则：gossip 额外信号（结合 GOSSIP_PATTERNS 使用）
EXTRA_GOSSIP_PATTERNS = re.compile(
    r'\b(celebrity|rumor|rumors|scandal|dating)\b|'
    r'(八卦|恋情|绯闻|出轨)',
    re.I
)

EVENT_TYPE_PRIORITY = {
    'new_model': -1,
    'product_launch': 0,
    'policy': 1,
    'gossip': 2,
    None: 3
}


def score_commercial(term: str, context: str = '', intent: str = None) -> int:
    """计算商业价值分 commercial_score (0-10 整数)
    - 高价值信号词匹配：term 中出现 +2，仅在 context 中出现 +1
    - 工具型意图 (intent==tool) 额外 +3；资讯型 +1
    - 纯吃瓜/纯人名相关 (GOSSIP_PATTERNS 或 is_person_name) 减 3，最低 0
    - 最终分数钳制到 0-10
    """
    if intent is None:
        intent = classify_intent(term, context)

    score = 0
    term_lower = term.lower()
    context_lower = context.lower()

    # 1. 高价值英文词匹配
    for pat in HIGH_VALUE_EN_PATTERNS:
        if pat.search(term_lower):
            score += 2
        elif pat.search(context_lower):
            score += 1

    # 2. 高价值中文词匹配
    for zh_kw in HIGH_VALUE_ZH:
        if zh_kw in term_lower:
            score += 2
        elif zh_kw in context_lower:
            score += 1

    # 3. 意图加分
    if intent == 'tool':
        score += 3
    elif intent == 'info':
        score += 1

    # 4. 吃瓜/人名低分惩罚
    combined = f"{term} {context}"
    if GOSSIP_PATTERNS.search(combined) or is_person_name(term, context):
        score -= 3

    # 5. 钳制到 0-10
    return max(0, min(10, score))


def classify_event_type(term: str, context: str = '') -> str:
    """根据 term 与 context 标记事件类型 ('product_launch' | 'policy' | 'gossip' | None)
    优先级: product_launch > policy > gossip；都没命中则返回 None
    """
    combined = f"{term} {context}"
    if PRODUCT_LAUNCH_PATTERN.search(combined):
        return 'product_launch'
    if POLICY_PATTERN.search(combined):
        return 'policy'
    if GOSSIP_PATTERNS.search(combined) or EXTRA_GOSSIP_PATTERNS.search(combined):
        return 'gossip'
    return None


def parse_heat(heat) -> int:
    """提取数字部分转 int（'500+'→500，'N/A'/无效→0）"""
    if heat is None:
        return 0
    if isinstance(heat, (int, float)):
        return int(heat)
    s = str(heat).strip().replace(',', '')
    m = re.search(r'\d+', s)
    if m:
        try:
            return int(m.group(0))
        except ValueError:
            return 0
    return 0


def get_sort_key(item: dict) -> tuple:
    """排序键：按 (event_type 优先级升序, commercial_score 降序, heat 降序)"""
    ev_type = item.get('event_type')
    prio = EVENT_TYPE_PRIORITY.get(ev_type, 3)
    c_score = item.get('commercial_score', 0)
    h_val = parse_heat(item.get('heat', 0))
    return (prio, -c_score, -h_val)


# ===================== seen_terms 状态管理 (v2) =====================
# seen_terms.json 不再是"推送过就永久跳过"的黑名单,而是状态库:
# 每个词(或同名不同产品)一条状态记录,保存首次/最近发现时间、来源集合、
# 通知历史、人工决策与发布结果。普通重复出现不推送;出现重大版本、热度跃升
# 或新增独立来源证据时,过了冷却期可再次提醒。
#
# 状态字段:
#   first_seen_at       首次发现时间(ISO,北京时间)
#   last_seen_at        最近一次出现时间
#   seen_count          累计出现次数
#   sources             出现过的来源集合(source 字段值)
#   project_url         项目地址(source_url),同名不同产品靠它区分
#   last_heat           最近一次热度数值(parse_heat 后)
#   last_version        最近一次识别到的版本号(如 "2.1")
#   last_notified_at    最近一次推送时间(未推送过为 null)
#   last_notified_reason first_time / heat_surge / new_source / major_version / legacy_migration
#                       注意: 只有最终进入输出 JSON 的词才写这两个字段(写文件成功后),
#                       被 Jev/变现分流过滤掉的词不算推送,写 last_filtered_*。
#   last_filtered_at    最近一次被过滤(Jev 产品技术度 / 变现 reject)的时间
#   last_filtered_reason 被过滤原因
#   jev                 最近一次 Jev 打分结果缓存(dict 或 null)
#   jev_scored_at       Jev 缓存打分时间(ISO)
#   jev_model           Jev 缓存所用模型名
#   decision            人工决策:null / build / watch / reject(由人工或上层流程回填,脚本不覆盖)
#   published_url       已发布页面 URL(发布后回填,脚本不覆盖)

RENOTIFY_COOLDOWN_DAYS = 7      # 再提醒冷却时间(天)
HEAT_SURGE_MULTIPLIER = 2.0     # 热度跃升判定倍数
_VERSION_RE = re.compile(r'\bv?(\d+)\.(\d+)(?:\.(\d+))?\b', re.IGNORECASE)


def _empty_seen_entry(now_iso=None) -> dict:
    return {
        'first_seen_at': now_iso,
        'last_seen_at': now_iso,
        'seen_count': 0,
        'sources': [],
        'project_url': None,
        'last_heat': 0,
        'last_version': None,
        'last_notified_at': None,
        'last_notified_reason': None,
        'last_filtered_at': None,
        'last_filtered_reason': None,
        'jev': None,
        'jev_scored_at': None,
        'jev_model': None,
        'decision': None,
        'published_url': None,
    }


def migrate_seen_entry(entry) -> dict:
    """把 v1 结构 {first_seen, count} 迁移到 v2 状态字段;已是 v2 的只补齐缺失字段。"""
    if not isinstance(entry, dict):
        entry = {}
    if 'first_seen_at' in entry:
        merged = _empty_seen_entry(entry.get('first_seen_at'))
        merged.update(entry)
        return merged
    first = entry.get('first_seen')
    migrated = _empty_seen_entry(first)
    migrated['last_seen_at'] = first
    migrated['seen_count'] = entry.get('count', 0) or 0
    # v1 语义:能进 seen_terms 就等于当年被输出/通知过
    migrated['last_notified_at'] = first
    migrated['last_notified_reason'] = 'legacy_migration'
    return migrated


def seen_needs_migration(data: dict) -> bool:
    return any(isinstance(v, dict) and 'first_seen_at' not in v for v in data.values())


def backup_seen_terms(path: str):
    """迁移前备份旧 seen_terms.json,返回备份路径"""
    if os.path.exists(path):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        bak = f"{path}.bak-{stamp}"
        shutil.copy2(path, bak)
        print(f"[状态库] 已备份旧 seen_terms.json -> {bak}")
        return bak
    return None


def load_seen_terms(path: str) -> dict:
    """加载已推送历史词库"""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"[去重] 读取 seen_terms.json 失败: {e}，将初始化为空字典")
    return {}


def save_seen_terms(path: str, data: dict):
    """保存已推送历史词库"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resolve_seen_key(term: str, project_url: str, seen: dict) -> str:
    """同名词条的 key:同一产品按 project_url 对齐;同名但项目地址不同的产品
    拆成 base、base#2、base#3…,不再只靠小写词粗暴合并。"""
    base = term.strip().lower()
    if not project_url:
        return base
    n = 1
    while True:
        key = base if n == 1 else f"{base}#{n}"
        entry = seen.get(key)
        if entry is None:
            return key
        if (entry.get('project_url') or '') == project_url:
            return key
        n += 1


def extract_version(text: str):
    """从词或说明文本里抽版本号,返回 (主版本 int 或 None, 版本串如 '2.1' 或 None)"""
    if not text:
        return None, None
    m = _VERSION_RE.search(text)
    if not m:
        return None, None
    return int(m.group(1)), m.group(0).lstrip('vV')


def should_notify(item: dict, entry, now: datetime) -> tuple:
    """判断本轮是否要推送该词,返回 (是否推送, 原因)。
    普通重复不推送;首次发现,或出现重大版本 / 热度跃升 / 新增独立来源证据,
    且距上次推送已过冷却期,才再次提醒。"""
    if entry is None or not entry.get('last_notified_at'):
        return True, 'first_time'
    try:
        last_notified = datetime.fromisoformat(entry['last_notified_at'])
    except Exception:
        return True, 'first_time'
    if now - last_notified < timedelta(days=RENOTIFY_COOLDOWN_DAYS):
        return False, 'cooldown'
    # 热度跃升:本轮热度达到上次记录的 HEAT_SURGE_MULTIPLIER 倍
    last_heat = entry.get('last_heat') or 0
    cur_heat = parse_heat(item.get('heat', 0))
    if last_heat > 0 and cur_heat >= last_heat * HEAT_SURGE_MULTIPLIER:
        return True, 'heat_surge'
    # 新增独立来源证据
    src = item.get('source')
    if src and src not in (entry.get('sources') or []):
        return True, 'new_source'
    # 重大版本:主版本号变化
    major, _ver = extract_version(f"{item.get('term', '')} {item.get('note', '')}")
    last_major, _ = extract_version(entry.get('last_version') or '')
    if major is not None and last_major is not None and major != last_major:
        return True, 'major_version'
    return False, 'repeat_no_new_evidence'


def mark_run_results(final_items: list, filtered_items: list, seen: dict, now_iso: str):
    """输出 JSON 写入成功后才调用:给最终输出的词写 last_notified_*,
    给被过滤掉的词写 last_filtered_*(不算推送,冷却期不拦它下次凭新证据再来)。

    final_items: 进入输出 JSON 的候选项(带 _seen_key / _notify_reason 临时字段)
    filtered_items: [(item, 过滤原因)] —— Jev 产品技术度过滤、变现分流 reject 的词
    """
    for item in final_items:
        entry = seen.get(item.get('_seen_key') or '')
        if entry is None:
            continue
        entry['last_notified_at'] = now_iso
        entry['last_notified_reason'] = item.get('_notify_reason')
    for item, reason in filtered_items:
        entry = seen.get(item.get('_seen_key') or '')
        if entry is None:
            continue
        entry['last_filtered_at'] = now_iso
        entry['last_filtered_reason'] = reason


def check_candidate(term: str, context: str = '') -> tuple:
    """候选词过滤规则检查，返回 (是否合格: bool, 过滤原因: str)"""
    clean_term = term.strip().lower()
    if not clean_term or len(clean_term) < 2:
        return False, "长度过短"

    # 过滤单个感叹词或纯数字
    if JUNK_PATTERNS.match(clean_term):
        return False, "无搜索意图感叹词/数字/单字符"

    # 过滤口语寒暄与无意义短语黑名单 (USELESS_PHRASES)
    if clean_term in USELESS_PHRASES or CONVERSATIONAL_PATTERNS.match(clean_term):
        return False, "无意义/寒暄黑名单短语"

    # 过滤纯人名八卦 / 明星恋情
    combined = f"{clean_term} {context}"
    if GOSSIP_PATTERNS.search(combined):
        return False, "纯人名八卦/明星恋情"

    # 过滤纯人名（名+姓结构，且无工具/产品/教程类信号）
    if is_person_name(term, context):
        return False, "纯人名资讯/人名(名+姓)"

    return True, "合格"


def is_valid_candidate(term: str, context: str = '') -> bool:
    """过滤规则检查封装"""
    valid, _ = check_candidate(term, context)
    return valid


# ---------------------------------------------------------------------------
# Jev 打分层 (结构化决策模型 TypeSafe Jev)
# 用 Jev 给候选词打三维分:
#   is_product_tech (noul 0-1): 是否具体产品/技术名
#   intent (choice): paid_tool / tech_term / company / media_generation / generic
#   newsworthiness (score 0-1): 建站价值
# 通道可切换(JEV_PROVIDER 环境变量):
#   openrouter(默认): OpenRouter Decisions API,模型 typesafe/jev-1.14,
#                     密钥只读环境变量 OPENROUTER_API_KEY;无 key 或调用失败
#                     自动回退 opencode_free
#   opencode_free:    OpenCode Zen 免费通道(原有通道,匿名 public key)
#   none:             不打分,全部词 jev_status=unscored 放行
# 注意:
#   - OpenCode 会拦 Python-urllib 默认 UA, 必须伪装 User-Agent
#   - 失败时静默返回 None, 绝不阻塞主流程;密钥不进代码/日志/输出
# ---------------------------------------------------------------------------
JEV_PROVIDER = os.environ.get("JEV_PROVIDER", "openrouter").strip().lower()
JEV_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
JEV_OPENROUTER_MODEL = "typesafe/jev-1.14"
JEV_ENDPOINT = "https://opencode.ai/zen/v1/systemone"
JEV_MODEL = "jev-1.13-free"
JEV_MAX_SCORE_PER_RUN = 15       # 每轮最多打分词数, 控制耗时
JEV_PER_SOURCE_MIN = 3           # 打分名额按来源分配时,每来源至少保留的名额
JEV_PRODUCT_TECH_THRESHOLD = 0.6 # is_product_tech 低于此值丢弃(可配置常量)
JEV_CACHE_TTL_DAYS = 7           # Jev 结果缓存有效期(天)
JEV_STATE_MAX_CHARS = 1500       # 传给 Jev 的上下文最大长度


def _jev_questions() -> dict:
    """Jev 问题集(两个通道共用同一份,保证结果结构一致)。"""
    return {
        "is_product_tech": {
            "type": "noul",
            "instructions": (
                "Is this term a specific product name, technology, tool, "
                "framework, model, or company — rather than a generic word "
                "or common phrase? Answer yes only if it names something concrete."
            ),
        },
        "intent": {
            "type": "choice",
            "instructions": "What is the commercial nature of this term?",
            "criteria": {
                "paid_tool": "A specific tool, SaaS, app, or service users would pay for or sign up to use",
                "tech_term": "A technical concept, framework, model, or standard with no direct paid product",
                "company": "A company or organization name",
                "media_generation": "An AI model or service that generates images, video, or audio/speech, which users may pay for per use or with credits",
                "generic": "A generic word, meme, event, or phrase with no clear commercial angle",
            },
        },
        "newsworthiness": {
            "type": "score",
            "instructions": (
                "How valuable is this as a 'new trending keyword' for building "
                "a niche tool/information site around? Consider: novelty, "
                "search-intent clarity, low-competition potential."
            ),
            "criteria": ["Low", "Medium", "High", "Exceptional"],
        },
    }


def _as_float(v):
    return float(v) if isinstance(v, (int, float)) else None


def _normalize_score(answer: dict):
    """把 Jev score 型答案归一化到 0-1。
    Decisions API 可能返回原始刻度值(如 2.38 / legend 0..3),System One
    通道实测返回 0-1;只对越界值(>1)按 legend 最大刻度(缺省 3)缩放,
    0-1 内的值原样保留,与旧行为兼容。"""
    s = answer.get("score")
    if not isinstance(s, (int, float)):
        return None
    s = float(s)
    if s > 1.0:
        max_idx = None
        legend = answer.get("legend")
        if isinstance(legend, dict) and legend:
            try:
                max_idx = max(float(k) for k in legend.keys())
            except Exception:
                max_idx = None
        if not max_idx:
            max_idx = 3.0  # criteria 共 4 档: Low/Medium/High/Exceptional
        if max_idx > 0:
            s = s / max_idx
    return max(0.0, min(1.0, s))


def _parse_jev_answers(data) -> dict | None:
    """把 Jev 原始返回统一成 {is_product_tech, intent, intent_confidence, newsworthiness}。
    返回为空/结构不对时返回 None(视为本条未打分)。"""
    if not isinstance(data, dict):
        return None
    answers = data.get("answers") or {}
    if not isinstance(answers, dict) or not answers:
        return None
    is_pt = answers.get("is_product_tech") or {}
    intent = answers.get("intent") or {}
    news = answers.get("newsworthiness") or {}
    return {
        "is_product_tech": _as_float(is_pt.get("noul")),
        "intent": intent.get("choice"),
        "intent_confidence": _as_float(intent.get("confidence")),
        "newsworthiness": _normalize_score(news) if isinstance(news, dict) else None,
    }


def _jev_post(endpoint: str, model: str, api_key: str, state: str, timeout: float, label: str) -> dict | None:
    """向 Jev 接口发一次请求并解析;失败返回 None。密钥只进请求头,不打印。"""
    import urllib.request

    payload = {"model": model, "state": state, "questions": _jev_questions()}
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "curl/8.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_jev_answers(data)
    except Exception as e:
        print(f"[Jev] {label} 打分失败: {e}")
        return None


def _jev_via_opencode(state: str, timeout: float = 30.0):
    """OpenCode Zen 免费通道(现有通道)。返回 (jev_dict|None, model|None)。"""
    r = _jev_post(JEV_ENDPOINT, JEV_MODEL, "public", state, timeout, "opencode_free")
    return (r, JEV_MODEL) if r else (None, None)


def score_with_jev_state(state: str, timeout: float = 30.0):
    """按 JEV_PROVIDER 走通道链给一段上下文打分,返回 (jev_dict|None, model|None)。
    openrouter 无 key 或调用失败 → 自动回退 opencode_free;都失败 → (None, None)。"""
    if JEV_PROVIDER == "none":
        return None, None
    if JEV_PROVIDER == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if api_key:
            r = _jev_post(
                JEV_OPENROUTER_ENDPOINT, JEV_OPENROUTER_MODEL, api_key,
                state, timeout, "openrouter",
            )
            if r:
                return r, JEV_OPENROUTER_MODEL
            print("[Jev] OpenRouter 通道失败,回退 opencode_free")
        else:
            print("[Jev] 未配置 OPENROUTER_API_KEY,回退 opencode_free")
        return _jev_via_opencode(state, timeout)
    if JEV_PROVIDER != "opencode_free":
        print(f"[Jev] 未知 JEV_PROVIDER={JEV_PROVIDER},按 opencode_free 处理")
    return _jev_via_opencode(state, timeout)


def score_with_jev(term: str, timeout: float = 30.0) -> dict | None:
    """用 Jev 给单个候选词打分(仅 term 作上下文,兼容旧调用),失败返回 None。"""
    jev, _model = score_with_jev_state(term, timeout=timeout)
    return jev


def build_jev_state(item: dict) -> str:
    """给 Jev 的上下文:不再只传 term,拼接来源、简介、note(含 Reddit 原帖标题)、
    HF 的 pipeline_tag/license、来源链接等证据,总长度截断到约 1500 字符。"""
    parts = [f"Term: {item.get('term', '')}"]
    if item.get('source'):
        parts.append(f"Source: {item['source']}")
    if item.get('description'):
        parts.append(f"Description: {item['description']}")
    if item.get('pipeline_tag'):
        parts.append(f"Pipeline: {item['pipeline_tag']}")
    if item.get('license'):
        parts.append(f"License: {item['license']}")
    if item.get('note'):
        parts.append(f"Note: {item['note']}")
    if item.get('source_url'):
        parts.append(f"URL: {item['source_url']}")
    return "\n".join(parts)[:JEV_STATE_MAX_CHARS]


def _select_for_scoring(ordered: list, limit: int) -> list:
    """从已排序的候选里挑打分名单:先保证每个来源至少 JEV_PER_SOURCE_MIN 个名额,
    再按全局排序补满到 limit,避免排在后面的来源整批拿不到分。"""
    if len(ordered) <= limit:
        return list(ordered)
    chosen, chosen_ids = [], set()
    by_source = {}
    for it in ordered:
        by_source.setdefault(it.get('source', ''), []).append(it)
    for _src, items in by_source.items():
        for it in items[:JEV_PER_SOURCE_MIN]:
            if len(chosen) >= limit:
                break
            chosen.append(it)
            chosen_ids.add(id(it))
    for it in ordered:
        if len(chosen) >= limit:
            break
        if id(it) not in chosen_ids:
            chosen.append(it)
            chosen_ids.add(id(it))
    return chosen


def _jev_cache_fresh(entry, now: datetime) -> bool:
    """seen 条目里的 Jev 缓存是否在有效期内。"""
    if not entry or not entry.get('jev') or not entry.get('jev_scored_at'):
        return False
    try:
        scored_at = datetime.fromisoformat(entry['jev_scored_at'])
    except Exception:
        return False
    return (now - scored_at) < timedelta(days=JEV_CACHE_TTL_DAYS)


def _score_one_with_jev(item: dict, seen, now: datetime, stats: dict):
    """给单个候选项打分(或复用缓存),结果写入 item['jev'] / item['jev_status']。
    异常在本函数内吞掉并计入 stats['failed'],不影响其他词。"""
    term = item.get('term', '')
    key = item.get('_seen_key')
    entry = seen.get(key) if (seen is not None and key) else None
    reason = item.get('_notify_reason') or ''

    # 2.4 缓存复用:7 天内、且本轮没有新证据(should_notify 给的是 cooldown/repeat)
    if _jev_cache_fresh(entry, now) and (reason == 'cooldown' or reason.startswith('repeat')):
        item['jev'] = entry['jev']
        item['jev_status'] = 'cached'
        stats['cached'] += 1
        print(f"[Jev] {term}: 复用缓存打分 (scored_at={entry.get('jev_scored_at')}, model={entry.get('jev_model')})")
        return

    stats['attempted'] += 1
    jev, model_used = score_with_jev_state(build_jev_state(item))
    if jev:
        item['jev'] = jev
        item['jev_status'] = 'scored'
        stats['succeeded'] += 1
        if entry is not None:
            entry['jev'] = jev
            entry['jev_scored_at'] = now.isoformat()
            entry['jev_model'] = model_used
        news = jev.get('newsworthiness')
        news_s = f"{news:.2f}" if isinstance(news, (int, float)) else "n/a"
        print(f"[Jev] {term}: intent={jev.get('intent')} news={news_s} via {model_used}")
    else:
        item['jev_status'] = 'unscored'
        stats['failed'] += 1
        print(f"[Jev] {term}: 本条未打分(通道失败),放行并标记 unscored")
    time.sleep(1)  # 免费池限流, 礼貌间隔


def enrich_with_jev(candidates: list, seen: dict = None, now: datetime = None) -> dict:
    """给候选词批量加 Jev 打分,返回本轮 jev_stats。
    先按现有规则(get_sort_key)排序再按来源分配名额;每个词单独容错。
    所有候选都会被标 jev_status: scored / cached / unscored。"""
    stats = {'provider': JEV_PROVIDER, 'attempted': 0, 'succeeded': 0, 'failed': 0, 'cached': 0}
    if now is None:
        now = datetime.now(timezone.utc)
    for item in candidates:
        item['jev_status'] = 'unscored'
    if JEV_PROVIDER == 'none' or not candidates:
        return stats
    ordered = sorted(candidates, key=get_sort_key)
    for item in _select_for_scoring(ordered, JEV_MAX_SCORE_PER_RUN):
        if not item.get('term'):
            continue
        try:
            _score_one_with_jev(item, seen, now, stats)
        except Exception as e:
            stats['failed'] += 1
            item['jev_status'] = 'unscored'
            print(f"[Jev] {item.get('term')}: 打分异常 {e},跳过本条")
    return stats


def update_jev_health(stats: dict, run_at_iso: str, output_dir: str, dry_run: bool = False):
    """维护最近 5 轮 Jev 打分统计(outputs/jev_health.json,dry-run 不落盘)。
    连续 2 轮 attempted>0 且 succeeded=0 时返回告警文案,否则返回 None。"""
    path = os.path.join(output_dir, 'jev_health.json')
    history = []
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                history = data.get('history') or []
        except Exception:
            history = []
    current = {'run_at': run_at_iso}
    for k in ('provider', 'attempted', 'succeeded', 'failed', 'cached'):
        current[k] = stats.get(k)
    recent = (history + [current])[-5:]
    if not dry_run:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'history': recent}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Jev健康] 写入 {path} 失败: {e}")
    last_two = recent[-2:]
    if len(last_two) == 2 and all(
        (h.get('attempted') or 0) > 0 and (h.get('succeeded') or 0) == 0 for h in last_two
    ):
        return "⚠️ [Jev健康] Jev 打分失效，当前过滤为全部放行（连续 2 轮 succeeded=0）"
    return None


# ---------------------------------------------------------------------------
# 变现分流 (classify_monetization)
# Jev 打分之后、排序之前调用:每个候选必须落到一条变现路线上,
# 路线决定后续动作与排序优先级:
#   quick_paid_site  抢 48 小时窗口的积分制付费站(媒体生成新模型 + API 可用)
#   tool_page        并入 yuliusbox 做工具页
#   content          写 yuliuswiki 文章
#   watch            先观察,不动手
#   reject           不输出(记入 last_filtered_reason)
# ---------------------------------------------------------------------------
MONETIZATION_PATH_PRIORITY = {
    'quick_paid_site': 0,
    'tool_page': 1,
    'content': 2,
    'watch': 3,
}
MONETIZATION_GENERIC_NEWS_MAX = 0.4  # jev.intent=generic 且 newsworthiness 低于此值 → reject


def classify_monetization(item: dict) -> tuple:
    """给候选词定变现路线,返回 (monetization_path, path_reason),规则按顺序判断。"""
    jev = item.get('jev') or {}
    # 1. 媒体生成新模型且 API 可用 → 抢 48 小时窗口
    if item.get('event_type') == 'new_model' and item.get('api_available'):
        return 'quick_paid_site', '抢 48 小时窗口：积分制付费站（媒体生成新模型且 API 可用）'
    # 2. 工具/付费意图 → yuliusbox 工具页
    if item.get('intent') == 'tool' or jev.get('intent') in ('paid_tool', 'media_generation'):
        return 'tool_page', '工具/付费意图明确，并入 yuliusbox 工具页'
    # 3. 资讯/技术概念意图 → yuliuswiki 文章
    if item.get('intent') == 'info' or jev.get('intent') == 'tech_term':
        return 'content', '资讯/技术概念意图，写 yuliuswiki 文章'
    # 4. Jev 判定 generic 且价值分低 → reject(不输出)
    news = jev.get('newsworthiness')
    if jev.get('intent') == 'generic' and isinstance(news, (int, float)) and news < MONETIZATION_GENERIC_NEWS_MAX:
        return 'reject', f"jev_intent=generic 且 newsworthiness={news:.2f} < {MONETIZATION_GENERIC_NEWS_MAX}，无变现路线"
    # 5. 其余先观察
    return 'watch', '暂无明确变现路线，先观察'


# ---------------------------------------------------------------------------
# 数据源 2: Reddit rising
# ---------------------------------------------------------------------------
def extract_reddit_phrases(titles_with_scores):
    """从帖子标题中提取 2-4 词短语，并结合热度打分"""
    phrase_scores = Counter()
    phrase_sample_title = {}

    for title, score in titles_with_scores:
        # 去除 url 和标点
        clean_text = re.sub(r'https?://\S+', '', title)
        words = re.findall(r'[a-zA-Z0-9_\-\.]+', clean_text.lower())

        chunks = []
        current_chunk = []
        for w in words:
            w_clean = w.strip('.,;:-_')
            if not w_clean or len(w_clean) < 2 or w_clean in STOP_WORDS or JUNK_PATTERNS.match(w_clean):
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = []
            else:
                current_chunk.append(w_clean)
        if current_chunk:
            chunks.append(current_chunk)

        # 提取 2-4 词组合
        for chunk in chunks:
            for n in range(2, min(5, len(chunk) + 1)):
                for i in range(len(chunk) - n + 1):
                    phrase = ' '.join(chunk[i:i+n])
                    # 过滤无意义/寒暄黑名单短语
                    if phrase in USELESS_PHRASES or CONVERSATIONAL_PATTERNS.match(phrase):
                        continue
                    phrase_scores[phrase] += max(score, 1)
                    if phrase not in phrase_sample_title:
                        phrase_sample_title[phrase] = title

    return phrase_scores, phrase_sample_title


def fetch_reddit_rising():
    """抓取 Reddit rising 帖子标题，提取关键词短语"""
    results = []
    print("[Reddit] 开始抓取 r/all/rising...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }

    titles_with_scores = []
    try:
        # 直接 GET https://www.reddit.com/r/all/rising.json
        resp = requests.get('https://www.reddit.com/r/all/rising.json', headers=headers, timeout=10)
        if resp.status_code == 200:
            posts = resp.json().get('data', {}).get('children', [])
            for post in posts:
                pdata = post.get('data', {})
                title = pdata.get('title', '')
                score = pdata.get('score', 0)
                if title:
                    titles_with_scores.append((title, score))
            print(f"[Reddit] 直接接口成功获取 {len(titles_with_scores)} 篇帖子")
        else:
            print(f"[Reddit] 直接接口返回状态码 {resp.status_code}，尝试公共镜像兜底...")
            # 若受机房 IP 限制被拦截，使用公共 Reddit 镜像接口兜底获取最新上升贴
            r2 = requests.get('https://api.pullpush.io/reddit/search/submission/?size=30', timeout=10)
            if r2.status_code == 200:
                posts = r2.json().get('data', [])
                for p in posts:
                    title = p.get('title', '')
                    score = p.get('score', 1)
                    if title:
                        titles_with_scores.append((title, score))
                print(f"[Reddit] 公共镜像兜底成功获取 {len(titles_with_scores)} 篇帖子")
    except Exception as e:
        print(f"[Reddit] 抓取异常: {e}")
        return results

    if not titles_with_scores:
        print("[Reddit] 未获取到有效帖子数据")
        return results

    phrase_scores, phrase_sample_title = extract_reddit_phrases(titles_with_scores)

    # 排序并筛选 top 候选词
    for phrase, score in phrase_scores.most_common(30):
        sample_title = phrase_sample_title.get(phrase, '')
        valid, reason = check_candidate(phrase, sample_title)
        if not valid:
            print(f"[Reddit] 过滤掉{reason}: {phrase}")
            continue

        intent = classify_intent(phrase, sample_title)
        # 过滤无意图的泛词
        if intent == 'other' and len(phrase.split()) < 2:
            continue

        commercial_score = score_commercial(phrase, sample_title, intent=intent)
        event_type = classify_event_type(phrase, sample_title)

        results.append({
            "term": phrase,
            "source": "reddit",
            "heat": score,
            "intent": intent,
            "commercial_score": commercial_score,
            "event_type": event_type,
            "note": f"Reddit社区上升热帖高频词，原帖: \"{sample_title[:60]}...\"",
            "source_url": f"https://www.reddit.com/search/?q={urllib.parse.quote(phrase)}",
        })
        if len(results) >= 5:
            break

    print(f"[Reddit] 抓取完成，产出 {len(results)} 个合格新词")
    return results


# ---------------------------------------------------------------------------
# 数据源 3: HuggingFace 趋势模型 (Hugging Face Hub API)
# ---------------------------------------------------------------------------
HF_RECENT_DAYS = 7        # 只保留 created_at 在此天数内的新模型
HF_MAX_RESULTS = 5        # 每轮最多输出条数
HF_TRENDING_LIMIT = 50    # trendingScore 候选池大小
HF_MEDIA_PIPELINE_TAGS = {
    'text-to-video', 'image-to-video', 'text-to-image',
    'image-to-image', 'text-to-speech',
}
# 明确禁止商用的许可证特征(命中才算"明确禁止";未知/other 不算禁止)
_NONCOMMERCIAL_LICENSE_RE = re.compile(
    r'(non[- ]?commercial|by-nc|[-_\s]nc\b|research[- ]only|no[- ]commercial)',
    re.I,
)


def _license_blocks_commercial(license_str) -> bool:
    """许可证是否明确禁止商用(只认明确的 NC / research-only 类表述)。"""
    if not license_str:
        return False
    return bool(_NONCOMMERCIAL_LICENSE_RE.search(str(license_str)))


def _hf_model_license(m):
    """从模型卡 cardData.license 读许可证,缺失时回退 tags 里的 license: 前缀。"""
    cd = getattr(m, 'card_data', None)
    if cd:
        d = cd.to_dict() if hasattr(cd, 'to_dict') else (cd if isinstance(cd, dict) else {})
        lic = d.get('license')
        if lic:
            return str(lic)
    for t in (getattr(m, 'tags', None) or []):
        ts = str(t)
        if ts.startswith('license:'):
            return ts.split(':', 1)[1]
    return None


def fetch_huggingface_trending():
    """抓取 Hugging Face 新模型信号:只用 trendingScore 排序拉候选,只保留
    created_at 在 7 天内的模型(不用 last_modified,避免老模型更新混入);
    一个模型输出一条结果,term = 模型名(去组织前缀、保留版本号与连字符,
    如 Wan2.5-T2V),不再把模型名/标签拆成 2-4 词泛短语。
    媒体生成类 pipeline + API 可用 + 许可证未明确禁止商用 → event_type=new_model。
    """
    results = []
    print("[HuggingFace] 开始抓取 trendingScore 新模型...")

    cutoff = datetime.now(timezone.utc) - timedelta(days=HF_RECENT_DAYS)
    api = HfApi()

    expand_fields = ['createdAt', 'pipeline_tag', 'cardData', 'likes', 'downloads']
    # api_available: 先确认已装的 huggingface_hub 支持 inferenceProviderMapping
    # 展开;不支持(抛错)就降级重试并把 api_available 置 None,不猜
    api_available_supported = True
    try:
        models = list(api.list_models(
            sort='trendingScore',
            limit=HF_TRENDING_LIMIT,
            expand=expand_fields + ['inferenceProviderMapping'],
        ))
    except Exception as e:
        print(f"[HuggingFace] inferenceProviderMapping 展开不受支持({e}),降级重试,api_available 置 null")
        api_available_supported = False
        try:
            models = list(api.list_models(
                sort='trendingScore',
                limit=HF_TRENDING_LIMIT,
                expand=expand_fields,
            ))
        except Exception as e2:
            print(f"[HuggingFace] 获取 trending 模型失败: {e2}")
            return results

    if not models:
        print("[HuggingFace] 未获取到模型数据")
        return results

    recent = [m for m in models if getattr(m, 'created_at', None) and m.created_at >= cutoff]
    print(f"[HuggingFace] trendingScore 候选 {len(models)} 个,其中 created_at 在 {HF_RECENT_DAYS} 天内的 {len(recent)} 个")

    for m in recent:
        model_name = (m.id or '').split('/')[-1]
        if not model_name:
            continue
        pipeline_tag = getattr(m, 'pipeline_tag', None)
        license_str = _hf_model_license(m)

        if api_available_supported:
            ipm = getattr(m, 'inference_provider_mapping', None)
            if ipm is None:
                api_available = None
            else:
                api_available = any(getattr(x, 'status', None) == 'live' for x in ipm)
        else:
            api_available = None

        heat = max(m.likes or 0, (m.downloads or 0) // 100, 1)
        context = f"huggingface model {m.id} pipeline:{pipeline_tag or ''} license:{license_str or ''}"

        valid, reason = check_candidate(model_name, context)
        if not valid:
            print(f"[HuggingFace] 过滤掉{reason}: {model_name}")
            continue

        intent = classify_intent(model_name, context)
        commercial_score = score_commercial(model_name, context, intent=intent)
        event_type = classify_event_type(model_name, context)
        if (
            pipeline_tag in HF_MEDIA_PIPELINE_TAGS
            and api_available
            and not _license_blocks_commercial(license_str)
        ):
            event_type = 'new_model'

        results.append({
            "term": model_name,
            "source": "huggingface",
            "heat": heat,
            "intent": intent,
            "commercial_score": commercial_score,
            "event_type": event_type,
            "note": f"HuggingFace 近{HF_RECENT_DAYS}天新模型 {m.id}(pipeline_tag={pipeline_tag}, license={license_str}, api_available={api_available})",
            "source_url": f"https://huggingface.co/{m.id}",
            "hf_model_id": m.id,
            "pipeline_tag": pipeline_tag,
            "license": license_str,
            "api_available": api_available,
        })
        if len(results) >= HF_MAX_RESULTS:
            break

    print(f"[HuggingFace] 抓取完成，产出 {len(results)} 个合格新词")
    return results


# ---------------------------------------------------------------------------
# 数据源 4: GitHub breakout 仓库 (GitHub Search API)
# ---------------------------------------------------------------------------
def fetch_github_breakout():
    """抓取 GitHub 新建仓库 breakout 信号：创建不足 14 天、日均涨星 200+ 或总星 400+。
    仓库名本身即产品/技术新词（如 docling、browser-use），用简介做上下文。
    未认证限流 10 req/min，本轮只打 1 次请求。
    """
    results = []
    print("[GitHub] 开始抓取 breakout 仓库...")

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_str = cutoff.strftime('%Y-%m-%d')
    url = 'https://api.github.com/search/repositories'
    params = {
        'q': f'created:>{cutoff_str} stars:>100',
        'sort': 'stars',
        'order': 'desc',
        'per_page': 30,
    }
    headers = {
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'trend-watch/1.0',
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        items = r.json().get('items', [])
    except Exception as e:
        print(f"[GitHub] 搜索 API 失败: {e}")
        return results

    print(f"[GitHub] 搜索返回 {len(items)} 个候选仓库")
    now = datetime.now(timezone.utc)

    # 先收集全部 breakout 仓库，再按 org 分组做套件去重
    qualified = []
    for repo in items:
        try:
            created = datetime.fromisoformat(repo['created_at'].replace('Z', '+00:00'))
        except Exception:
            continue
        age_days = max((now - created).total_seconds() / 86400.0, 0.5)
        if age_days > 14:
            continue
        stars = repo.get('stargazers_count', 0) or 0
        stars_per_day = stars / age_days

        # breakout 判定：日均 200+ 星，或总星 400+（单日爆发型）
        if not (stars_per_day >= 200 or stars >= 400):
            continue

        raw_name = repo.get('name', '')
        # 仓库名清洗：分隔符转空格，保留原名做 note
        term = re.sub(r'[-_]+', ' ', raw_name).strip()
        description = (repo.get('description') or '').strip()
        context = f"{description} stars:{stars}"
        org = (repo.get('full_name') or '/').split('/')[0]

        valid, reason = check_candidate(term, context)
        if not valid:
            print(f"[GitHub] 过滤掉{reason}: {term}")
            continue

        qualified.append({
            'repo': repo, 'term': term, 'context': context, 'org': org,
            'stars': stars, 'stars_per_day': stars_per_day, 'age_days': age_days,
        })

    # 套件去重：同一 org 在本轮出现 ≥3 个 breakout 仓库时，只发一个套件级候选，
    # 避免逐词建出高度雷同的 doorway pages（如 storytold 一次推 10 款 *craft）
    by_org = {}
    for q in qualified:
        by_org.setdefault(q['org'], []).append(q)

    def emit_single(q):
        intent = classify_intent(q['term'], q['context'])
        commercial_score = score_commercial(q['term'], q['context'], intent=intent)
        event_type = classify_event_type(q['term'], q['context'])
        full_name = q['repo'].get('full_name', '')
        results.append({
            "term": q['term'],
            "source": "github_breakout",
            "heat": round(q['stars_per_day']),
            "intent": intent,
            "commercial_score": commercial_score,
            "event_type": event_type,
            "note": f"GitHub breakout：{full_name} 建仓{q['age_days']:.0f}天获{q['stars']}星（日均{q['stars_per_day']:.0f}）",
            "source_url": f"https://github.com/{full_name}" if full_name else "",
            "description": (q['repo'].get('description') or '').strip(),
        })

    def emit_suite(org, members):
        members_sorted = sorted(members, key=lambda m: m['stars'], reverse=True)
        total_stars = sum(m['stars'] for m in members_sorted)
        member_list = '、'.join(
            f"{m['repo'].get('full_name','')}({m['stars']}星)" for m in members_sorted[:10]
        )
        suite_term = f"{org} suite"
        context = f"GitHub同org套件级爆发，共{len(members_sorted)}个仓库：{member_list}"
        valid, reason = check_candidate(suite_term, context)
        if not valid:
            print(f"[GitHub] 套件词过滤掉{reason}: {suite_term}，回退逐个输出")
            for m in members_sorted:
                emit_single(m)
            return
        intent = classify_intent(suite_term, context)
        commercial_score = score_commercial(suite_term, context, intent=intent)
        event_type = classify_event_type(suite_term, context)
        results.append({
            "term": suite_term,
            "source": "github_breakout",
            "heat": round(sum(m['stars_per_day'] for m in members_sorted)),
            "intent": intent,
            "commercial_score": commercial_score,
            "event_type": event_type,
            "note": f"GitHub套件级爆发：{org} 一次推出{len(members_sorted)}个仓库，累计{total_stars}星：{member_list}",
            "source_url": f"https://github.com/{org}",
            "description": context,
        })
        print(f"[GitHub] {org} 套件去重：{len(members_sorted)}个仓库合并为1个套件候选")

    for org, members in sorted(by_org.items(), key=lambda kv: -sum(m['stars'] for m in kv[1])):
        if len(members) >= 3:
            emit_suite(org, members)
        else:
            for m in members:
                emit_single(m)
        if len(results) >= 5:
            break

    print(f"[GitHub] 抓取完成，产出 {len(results)} 个合格新词")
    return results


# ---------------------------------------------------------------------------
# 数据源 5: GSC "第一次有展现" 新词 (Google Search Console Search Analytics)
# ---------------------------------------------------------------------------
def fetch_gsc_new_queries(update_baseline: bool = True):
    """GSC first-impression 新词发现：拉各站点近 14 天 query 数据，
    找出基线快照里从没出现过的新 query——这是真实用户搜到我们站的词，
    天然带搜索需求，直接做 yuliusbox 长尾页选题。
    借鉴 new-term-radar 的 Search Console first-impression 信号。
    """
    results = []
    print("[GSC] 开始抓取首次有展现的新 query...")

    try:
        import urllib.parse
        sys.path.insert(0, os.path.expanduser("~/workspace/gsc-api"))
        import gsc as gsc_client
    except Exception as e:
        print(f"[GSC] 导入 gsc 客户端失败: {e}")
        return results

    base_dir = os.path.dirname(os.path.abspath(__file__))
    baseline_path = os.path.join(base_dir, "hidden_files", "gsc_seen_queries.json")
    try:
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)
    except Exception:
        baseline = {}

    sites = [
        "https://www.yuliusbox.com/",
        "https://www.heic2jpg-free.com/",
        "https://yuliuswiki.com/",
    ]
    today = datetime.now(timezone.utc).date()
    end = (today - timedelta(days=2)).isoformat()      # GSC 数据滞后约 2 天
    start = (today - timedelta(days=16)).isoformat()

    try:
        token = gsc_client.get_access_token()
    except Exception as e:
        print(f"[GSC] 获取 access token 失败: {e}")
        return results

    new_terms = []
    for site in sites:
        try:
            enc = urllib.parse.quote(site, safe="")
            res = gsc_client.api_post(
                f"/sites/{enc}/searchAnalytics/query", token,
                {"startDate": start, "endDate": end,
                 "dimensions": ["query"], "rowLimit": 1000},
            )
        except Exception as e:
            print(f"[GSC] {site} 查询失败: {e}")
            continue
        seen = set(baseline.get(site, []))
        current = {}
        for row in res.get("rows", []):
            q = (row.get("keys") or [""])[0].strip().lower()
            if not q:
                continue
            current[q] = {
                "impressions": row.get("impressions", 0),
                "clicks": row.get("clicks", 0),
            }
        # 基线更新：并入本轮全部 query
        baseline[site] = sorted(set(baseline.get(site, [])) | set(current.keys()))
        for q, m in current.items():
            if q not in seen and m["impressions"] >= 2:  # 过滤单次展现噪音
                # 过滤 URL 型导航查询（如 "https://heic2jpg.com"）：无建页价值
                if re.search(r"https?://|www\.|\.(com|net|org|io|cn)\b", q):
                    continue
                new_terms.append((site, q, m))

    if update_baseline:
        try:
            os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
            with open(baseline_path, "w", encoding="utf-8") as f:
                json.dump(baseline, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[GSC] 基线写入失败: {e}")
    else:
        print("[GSC] dry-run: 跳过基线写入")

    new_terms.sort(key=lambda t: -t[2]["impressions"])
    print(f"[GSC] 发现 {len(new_terms)} 个首次有展现的新 query")

    for site, term, m in new_terms[:5]:
        context = (f"GSC首次展现: {site} 近14天展现{m['impressions']}次"
                   f"点击{m['clicks']}次")
        valid, reason = check_candidate(term, context)
        if not valid:
            print(f"[GSC] 过滤掉{reason}: {term}")
            continue
        intent = classify_intent(term, context)
        commercial_score = score_commercial(term, context, intent=intent)
        event_type = classify_event_type(term, context)
        results.append({
            "term": term,
            "source": "gsc_first_impression",
            "heat": m["impressions"],
            "intent": intent,
            "commercial_score": commercial_score,
            "event_type": event_type,
            "note": context,
            "source_url": f"https://www.google.com/search?q={urllib.parse.quote(term)}",
        })

    print(f"[GSC] 抓取完成，产出 {len(results)} 个合格新词")
    return results


# ---------------------------------------------------------------------------
# 数据源 6: X (Twitter) 信号源
# ---------------------------------------------------------------------------
def fetch_x_trends():
    """抓取 X (Twitter) 趋势或高互动推文。
    X 无公开免费 API：
    1. 优先尝试 Nitter 公开实例 RSS；
    2. 若不稳定，降级为尝试社交搜索抓取过去 8 小时 AI/科技圈高互动推文；
    3. 提取产品名、技术名词等 2-4 词关键词短语；
    4. 失败时静默跳过，不阻塞主流程。
    """
    results = []
    print("[X (Twitter)] 开始尝试获取 X 趋势信号...")

    tweets = []

    # 1. 尝试 Nitter 公开实例 RSS 检索
    nitter_instances = [
        "https://nitter.net",
        "https://nitter.poast.org",
        "https://nitter.privacydev.net",
        "https://xcancel.com"
    ]
    query = "AI OR LLM OR Tech"

    for instance in nitter_instances:
        try:
            url = f"{instance}/search/rss?f=tweets&q={requests.utils.quote(query)}"
            resp = requests.get(url, timeout=3, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            if resp.status_code == 200 and b'<rss' in resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall('.//item'):
                    title = item.find('title').text if item.find('title') is not None else ''
                    desc = item.find('description').text if item.find('description') is not None else ''
                    content = f"{title} {desc}".strip()
                    if content:
                        tweets.append((content, 10))
                if tweets:
                    print(f"[X (Twitter)] 通过 Nitter 实例 ({instance}) 成功获取 {len(tweets)} 条推文")
                    break
        except Exception:
            continue

    # 2. 若 Nitter 失败，尝试通过社交搜索接口降级抓取过去 8 小时 AI/科技圈推文
    if not tweets:
        try:
            social_search_fn = None
            try:
                import social  # type: ignore
                if hasattr(social, 'search'):
                    social_search_fn = social.search
            except ImportError:
                pass

            if social_search_fn:
                social_results = social_search_fn(query="AI tech", time_range="8h")
                for item in social_results:
                    text = item.get('text') or item.get('content') or ''
                    score = item.get('interaction') or item.get('likes') or 1
                    if text:
                        tweets.append((text, score))
        except Exception:
            # 降级调用失败静默忽略
            pass

    # 3. 失败时静默跳过，不阻塞主流程
    if not tweets:
        print("[X (Twitter)] 公开实例与社交搜索接口不可用，已静默跳过（不阻塞主流程）")
        return results

    # 4. 从推文中提取产品名、技术名词（2-4 词关键词短语）
    phrase_scores, phrase_sample = extract_reddit_phrases(tweets)

    for phrase, score in phrase_scores.most_common(20):
        sample = phrase_sample.get(phrase, '')
        valid, reason = check_candidate(phrase, sample)
        if not valid:
            continue

        intent = classify_intent(phrase, sample)
        if intent == 'other' and len(phrase.split()) < 2:
            continue

        c_score = score_commercial(phrase, sample, intent=intent)
        ev_type = classify_event_type(phrase, sample)

        results.append({
            "term": phrase,
            "source": "x",
            "heat": score,
            "intent": intent,
            "commercial_score": c_score,
            "event_type": ev_type,
            "note": f"X平台AI/科技高互动推文提取: \"{sample[:60]}...\""
        })
        if len(results) >= 5:
            break

    print(f"[X (Twitter)] 提取完成，产出 {len(results)} 个合格新词")
    return results


# ---------------------------------------------------------------------------
# 命中率追踪机制 (Hit Rate Tracking)
# ---------------------------------------------------------------------------
def load_tracking_data(path: str) -> list:
    """加载候选词命中率追踪文件 hidden_files/word_tracking.json"""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[命中率追踪] 读取 {path} 失败: {e}，将初始化为空列表")
    return []


def save_tracking_data(path: str, data: list):
    """保存候选词追踪记录到 hidden_files/word_tracking.json"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_discovered_terms(candidates: list, tracking_path: str, timestamp_iso: str):
    """将新发现的候选词追加写入 hidden_files/word_tracking.json；
    已在追踪中的词追加本轮热度到 heat_history（按天去重），并补 source_url。"""
    tracking_data = load_tracking_data(tracking_path)
    existing = {}
    for item in tracking_data:
        key = (item.get('term', '').strip().lower(), item.get('source', ''))
        existing[key] = item

    day = timestamp_iso[:10]
    changed = False
    new_count = 0
    for c in candidates:
        term = c.get('term', '').strip()
        source = c.get('source', '')
        key = (term.lower(), source)
        heat = parse_heat(c.get('heat', 0))
        url = c.get('source_url', '')
        if key not in existing:
            item = {
                "term": term,
                "discovered_at": timestamp_iso,
                "source": source,
                "initial_heat": heat,
                "heat_history": [{"date": day, "heat": heat}],
                "source_url": url,
                "checked": False,
                "hit": None,
            }
            tracking_data.append(item)
            existing[key] = item
            new_count += 1
            changed = True
        else:
            item = existing[key]
            # 老词补字段（历史数据无这些键）
            if not item.get("heat_history"):
                item["heat_history"] = [{
                    "date": (item.get("discovered_at") or timestamp_iso)[:10],
                    "heat": item.get("initial_heat", 0),
                }]
            hist = item["heat_history"]
            if not hist or hist[-1].get("date") != day:
                hist.append({"date": day, "heat": heat})
                changed = True
            if url and not item.get("source_url"):
                item["source_url"] = url
                changed = True

    if changed:
        save_tracking_data(tracking_path, tracking_data)
    if new_count > 0:
        print(f"[命中率追踪] 已记录 {new_count} 个新候选词至 {tracking_path}")
    else:
        print(f"[命中率追踪] 本批次候选词均已在追踪列表中，无需重复记录")


def check_hit_rate(tracking_path: str = None, output_dir: str = None) -> dict:
    """对 14 天前记录的候选词用 Google Trends 查询过去 14 天热度，
    计算每个信号源的命中率（14 天后热度 > 发现时 × 2 算命中），
    并输出 hit-rate 报告到 outputs/ 目录下。
    """
    print("\n=== 开始执行候选词命中率核验 (check_hit_rate) ===")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if tracking_path is None:
        tracking_path = os.path.join(base_dir, "hidden_files", "word_tracking.json")
    if output_dir is None:
        output_dir = os.path.join(base_dir, "outputs")

    os.makedirs(output_dir, exist_ok=True)

    tracking_data = load_tracking_data(tracking_path)
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(timezone.utc).astimezone(beijing_tz)

    if not tracking_data:
        print(f"[命中率核验] 追踪库 {tracking_path} 为空，输出初始报告")
        report = {
            "generated_at": now.isoformat(),
            "summary": "追踪词库为空，暂无数据",
            "overall": {
                "total_tracked": 0,
                "total_evaluated": 0,
                "total_hits": 0,
                "hit_rate": "0.0%"
            },
            "sources": {},
            "evaluated_details": []
        }
        _write_hit_rate_report(output_dir, report)
        return report

    # 筛选出达到 14 天周期的待核验词
    eval_candidates = []
    for item in tracking_data:
        disc_str = item.get('discovered_at', '')
        try:
            disc_dt = datetime.fromisoformat(disc_str)
            delta_days = (now - disc_dt).total_seconds() / 86400.0
            if delta_days >= 14:
                eval_candidates.append(item)
        except Exception:
            continue

    print(f"[命中率核验] 当前总追踪词数: {len(tracking_data)}，达到 14 天周期的词数: {len(eval_candidates)}")

    pt = TrendReq(hl='en-US', tz=360)

    # 对达到 14 天且尚未核验的词通过 Google Trends 验证热度趋势
    updated = False
    for item in eval_candidates:
        if item.get('checked'):
            continue

        term = item.get('term', '')
        try:
            time.sleep(1.2)  # 节流避免 Google 429 限流
            pt.build_payload([term], timeframe='today 1-m', geo='US')
            df = pt.interest_over_time()

            if not df.empty and term in df.columns:
                series = df[term].tolist()
                # 丢弃最近 2 天未结算数据：Trends 数据有延迟，直接用会导致 0→100 假峰值误判
                if len(series) > 2:
                    series = series[:-2]
                # 过去 14 天数据点窗口
                series_14d = series[-14:] if len(series) >= 14 else series
                heat_discovery = series_14d[0]
                heat_14d = series_14d[-1]

                # 命中判定规则：14 天后热度 > 发现时 × 2
                if heat_discovery > 0:
                    is_hit = bool(heat_14d > heat_discovery * 2)
                else:
                    # 若发现时热度为 0，只要 14 天后有明显热度（>0）即算爆发命中
                    is_hit = bool(heat_14d > 0)

                item['checked'] = True
                item['hit'] = is_hit
                item['heat_at_discovery'] = heat_discovery
                item['heat_after_14d'] = heat_14d
                item['check_time'] = now.isoformat()
                updated = True
                print(f"[命中率核验] 词 '{term}' 发现时热度: {heat_discovery}, 14天后热度: {heat_14d} -> {'命中' if is_hit else '未命中'}")
            else:
                item['checked'] = True
                item['hit'] = False
                item['heat_at_discovery'] = 0
                item['heat_after_14d'] = 0
                item['check_time'] = now.isoformat()
                updated = True
                print(f"[命中率核验] 词 '{term}' 无 Trends 数据，计为未命中")
        except Exception as e:
            print(f"[命中率核验] 查询 Google Trends 异常 ({term}): {e}，本次暂不标记")

    if updated:
        save_tracking_data(tracking_path, tracking_data)

    # 统计各信号源命中率
    source_stats = {}
    total_evaluated = 0
    total_hits = 0

    for item in tracking_data:
        if not item.get('checked'):
            continue
        src = item.get('source', 'unknown')
        if src not in source_stats:
            source_stats[src] = {'total': 0, 'hits': 0}
        source_stats[src]['total'] += 1
        total_evaluated += 1
        if item.get('hit'):
            source_stats[src]['hits'] += 1
            total_hits += 1

    for src, st in source_stats.items():
        st['hit_rate'] = f"{(st['hits'] / st['total'] * 100):.1f}%" if st['total'] > 0 else "0.0%"

    overall_rate = f"{(total_hits / total_evaluated * 100):.1f}%" if total_evaluated > 0 else "0.0%"

    report = {
        "generated_at": now.isoformat(),
        "overall": {
            "total_tracked": len(tracking_data),
            "total_evaluated": total_evaluated,
            "total_hits": total_hits,
            "hit_rate": overall_rate
        },
        "sources": source_stats,
        "evaluated_details": [
            {
                "term": it.get('term'),
                "source": it.get('source'),
                "discovered_at": it.get('discovered_at'),
                "heat_at_discovery": it.get('heat_at_discovery'),
                "heat_after_14d": it.get('heat_after_14d'),
                "hit": it.get('hit'),
                "check_time": it.get('check_time')
            }
            for it in tracking_data if it.get('checked')
        ]
    }

    _write_hit_rate_report(output_dir, report)
    print(f"[命中率核验] 核验完成：总评估 {total_evaluated} 词，命中 {total_hits} 词，总体命中率 {overall_rate}")
    return report


def _write_hit_rate_report(output_dir: str, report: dict):
    """输出命中率报告至 outputs/hit_rate_report.json 与 outputs/hit_rate_report.md"""
    json_path = os.path.join(output_dir, "hit_rate_report.json")
    md_path = os.path.join(output_dir, "hit_rate_report.md")

    # 1. 写入 JSON
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 2. 写入 Markdown
    md_lines = [
        "# 候选词 14 天命中率追踪报告 (Hit Rate Report)",
        f"- **生成时间**: {report.get('generated_at')}",
        f"- **总追踪候选词数**: {report['overall']['total_tracked']}",
        f"- **已完成 14 天核验词数**: {report['overall']['total_evaluated']}",
        f"- **总体命中率**: {report['overall']['hit_rate']} ({report['overall']['total_hits']} / {report['overall']['total_evaluated']})",
        "",
        "## 各信号源命中率统计",
        "| 信号源 (Source) | 评估总数 (Total) | 命中数 (Hits) | 命中率 (Hit Rate) |",
        "| :--- | :--- | :--- | :--- |"
    ]

    sources = report.get('sources', {})
    if sources:
        for src, st in sources.items():
            md_lines.append(f"| `{src}` | {st['total']} | {st['hits']} | **{st['hit_rate']}** |")
    else:
        md_lines.append("| 暂无数据 | 0 | 0 | 0.0% |")

    md_lines.append("")
    md_lines.append("## 已核验词详情")
    details = report.get('evaluated_details', [])
    if details:
        md_lines.append("| 关键词 (Term) | 信号源 | 发现时间 | 发现时热度 | 14天后热度 | 命中状态 |")
        md_lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for d in details:
            status_str = "✅ 命中" if d.get('hit') else "❌ 未命中"
            md_lines.append(
                f"| `{d.get('term')}` | `{d.get('source')}` | {d.get('discovered_at', '')[:10]} | "
                f"{d.get('heat_at_discovery')} | {d.get('heat_after_14d')} | {status_str} |"
            )
    else:
        md_lines.append("暂无已完成 14 天周期的关键词详情。")

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(md_lines) + "\n")

    print(f"[命中率报告] 已生成 JSON 报告: {json_path}")
    print(f"[命中率报告] 已生成 Markdown 报告: {md_path}")


# ---------------------------------------------------------------------------
# 主流程与文件输出
# ---------------------------------------------------------------------------
def main(dry_run: bool = False):
    print("=== 开始运行搜索新词监控 (trend_watch.py) ===")
    if dry_run:
        print("[dry-run] 本轮只写输出 JSON,不更新 seen_terms / word_tracking 等任何状态")
    all_candidates = []

    # 发现源优先级：GitHub breakout > HuggingFace > Reddit 技术区 > GSC 首次展现 > X（失败静默跳过）
    # Google Trends 已降为纯验证源（14 天命中率核验），不再参与发现

    # 1. GitHub breakout 仓库 (新增信号源)
    try:
        gh_results = fetch_github_breakout()
        all_candidates.extend(gh_results)
    except Exception as e:
        print(f"[GitHub] 处理发生未知异常: {e}")

    # 2. HuggingFace 趋势模型
    try:
        hf_results = fetch_huggingface_trending()
        all_candidates.extend(hf_results)
    except Exception as e:
        print(f"[HuggingFace] 处理发生未知异常: {e}")

    # 3. Reddit rising
    try:
        reddit_results = fetch_reddit_rising()
        all_candidates.extend(reddit_results)
    except Exception as e:
        print(f"[Reddit] 处理发生未知异常: {e}")

    # 4. X 趋势 (失败静默跳过)
    try:
        x_results = fetch_x_trends()
        all_candidates.extend(x_results)
    except Exception as e:
        print(f"[X] 处理发生未知异常: {e}")

    # 5. GSC 首次有展现的新 query（真实用户搜索，长尾页选题）
    try:
        gsc_results = fetch_gsc_new_queries(update_baseline=not dry_run)
        all_candidates.extend(gsc_results)
    except Exception as e:
        print(f"[GSC] 处理发生未知异常: {e}")

    # 确定输出文件路径（按北京时间 UTC+8 命名：YYYY-MM-DD-HH.json）
    beijing_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(timezone.utc).astimezone(beijing_tz)
    now_bj_iso = now_bj.isoformat()
    file_name = now_bj.strftime("%Y-%m-%d-%H.json")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(base_dir, "outputs")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, file_name)
    seen_path = os.path.join(output_dir, "seen_terms.json")
    tracking_path = os.path.join(base_dir, "hidden_files", "word_tracking.json")

    # 已处理词状态库:去重 + 再提醒判定(普通重复不推送,有新证据且过冷却期才再提醒)
    seen = load_seen_terms(seen_path)
    needs_migration = seen_needs_migration(seen) or any(
        isinstance(v, dict) and 'last_filtered_at' not in v for v in seen.values()
    )
    if needs_migration:
        backup_seen_terms(seen_path)
        seen = {k: migrate_seen_entry(v) for k, v in seen.items()}
        print(f"[状态库] seen_terms.json 已迁移到 v2 状态结构(共 {len(seen)} 条,旧文件已备份)")
    output_candidates = []

    for item in all_candidates:
        term = item.get('term', '')
        project_url = item.get('source_url') or ''
        key = resolve_seen_key(term, project_url, seen)
        entry = seen.get(key)
        notify, reason = should_notify(item, entry, now_bj)

        if entry is None:
            entry = _empty_seen_entry(now_bj_iso)
            seen[key] = entry
        # 无论本轮是否推送,都更新状态(决策/发布字段由人工回填,这里不覆盖)
        entry['last_seen_at'] = now_bj_iso
        entry['seen_count'] = (entry.get('seen_count') or 0) + 1
        src = item.get('source')
        if src and src not in (entry.get('sources') or []):
            entry.setdefault('sources', []).append(src)
        if project_url and not entry.get('project_url'):
            entry['project_url'] = project_url
        cur_heat = parse_heat(item.get('heat', 0))
        if cur_heat:
            entry['last_heat'] = cur_heat
        _maj, _ver = extract_version(f"{term} {item.get('note', '')}")
        if _ver:
            entry['last_version'] = _ver

        if not notify:
            print(f"[去重] 跳过词: {term}（{reason}）")
            continue
        # 只挂临时字段;last_notified_* 等输出 JSON 写成功后才由 mark_run_results 落标,
        # 避免被后续 Jev 过滤丢弃的词被误记为已推送、吃掉 7 天冷却期
        item['_seen_key'] = key
        item['_notify_reason'] = reason
        output_candidates.append(item)

    # Jev 打分层: 给 top 候选词加结构化评分(失败不阻塞)
    jev_stats = {'provider': JEV_PROVIDER, 'attempted': 0, 'succeeded': 0, 'failed': 0, 'cached': 0}
    try:
        jev_stats = enrich_with_jev(output_candidates, seen=seen, now=now_bj)
    except Exception as e:
        print(f"[Jev] 批量打分发生未知异常: {e}")

    # Jev 产品技术度过滤: 有有效评分且 is_product_tech < JEV_PRODUCT_TECH_THRESHOLD 的直接丢弃
    # (yuliuswiki 只做产品技术类词；无评分的不拦截，避免 Jev 失败时误杀)
    # 被丢弃的词收集到 filtered_items,输出写成功后写 last_filtered_*(不算推送)
    kept = []
    filtered_items = []
    for c in output_candidates:
        j = c.get('jev') or {}
        pt = j.get('is_product_tech')
        if isinstance(pt, (int, float)) and pt < JEV_PRODUCT_TECH_THRESHOLD:
            print(f"[Jev过滤] 丢弃: {c.get('term')} (is_product_tech={pt:.2f})")
            filtered_items.append((c, f"jev_product_tech={pt:.2f} < {JEV_PRODUCT_TECH_THRESHOLD}"))
            continue
        kept.append(c)
    print(f"[Jev过滤] {len(output_candidates)} -> {len(kept)}")
    output_candidates = kept

    # 变现分流:Jev 打分后、排序前给每个候选定路线;reject 的不输出(记 filtered)
    path_counts = {}
    routed = []
    for c in output_candidates:
        path, path_reason = classify_monetization(c)
        if path == 'reject':
            print(f"[变现分流] reject: {c.get('term')}（{path_reason}）")
            filtered_items.append((c, f"monetization_reject: {path_reason}"))
            continue
        c['monetization_path'] = path
        c['path_reason'] = path_reason
        path_counts[path] = path_counts.get(path, 0) + 1
        routed.append(c)
    output_candidates = routed
    print(f"[变现分流] 各路线条数: {path_counts}")

    # 排序:先路线优先级(quick_paid_site > tool_page > content > watch),再原 get_sort_key
    output_candidates.sort(key=lambda it: (
        MONETIZATION_PATH_PRIORITY.get(it.get('monetization_path'), 3),
        get_sort_key(it),
    ))

    # 写入 JSON 文件(剥掉 _ 开头的内部临时字段)
    out_items = [
        {k: v for k, v in it.items() if not k.startswith('_')}
        for it in output_candidates
    ]
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(out_items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[输出] 写入 {output_path} 失败: {e},本轮不更新任何状态")
        return

    # 输出写成功后才落标:最终输出的词写 last_notified_*,被过滤的词写 last_filtered_*
    if dry_run:
        print(f"[dry-run] 跳过状态更新(本应标记 notified {len(output_candidates)} 个、filtered {len(filtered_items)} 个)")
    else:
        mark_run_results(output_candidates, filtered_items, seen, now_bj_iso)

        # 保存 seen_terms.json
        save_seen_terms(seen_path, seen)

        # 命中率追踪：记录新发现候选词至 hidden_files/word_tracking.json
        record_discovered_terms(out_items, tracking_path, now_bj_iso)

        # 命中率核验：核验 14 天前的候选词并生成报告至 outputs/
        try:
            check_hit_rate(tracking_path=tracking_path, output_dir=output_dir)
        except Exception as e:
            print(f"[命中率核验] 执行发生未知异常: {e}")

    # Jev 健康监控:打印本轮打分统计,连续 2 轮打分全失败时明确告警
    print(f"[Jev统计] provider={jev_stats.get('provider')} attempted={jev_stats.get('attempted')} "
          f"succeeded={jev_stats.get('succeeded')} failed={jev_stats.get('failed')} cached={jev_stats.get('cached')}")
    jev_warning = update_jev_health(jev_stats, now_bj_iso, output_dir, dry_run=dry_run)
    if jev_warning:
        print(jev_warning)

    print(f"\n=== 监控完成 ===")
    print(f"有效候选新词总数: {len(output_candidates)}")
    by_source = {}
    for c in out_items:
        src = c.get('source', 'unknown')
        by_source[src] = by_source.get(src, 0) + 1
    print(f"各来源条数: {by_source}")
    print(f"各路线条数: {path_counts}")
    quick = [c for c in out_items if c.get('monetization_path') == 'quick_paid_site']
    if quick:
        print("\n--- 🚀 quick_paid_site（抢 48 小时窗口：积分制付费站） ---")
        for c in quick:
            print(f"- {c.get('term')} | 模型类型: {c.get('pipeline_tag')} | "
                  f"api_available: {c.get('api_available')} | license: {c.get('license')} | {c.get('source_url')}")
    print(f"输出文件路径: {output_path}")
    print(f"去重库路径: {seen_path}")
    print(f"追踪库路径: {tracking_path}")
    print("\n--- 结果预览 ---")
    print(json.dumps(out_items, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    import argparse

    _ap = argparse.ArgumentParser(description="搜索新词监控")
    _ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只写输出 JSON,不更新 seen_terms / word_tracking / GSC 基线等任何状态",
    )
    main(dry_run=_ap.parse_args().dry_run)

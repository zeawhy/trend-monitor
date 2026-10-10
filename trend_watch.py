"""
trend_watch.py - 新词监控脚本，用于发现刚冒头的搜索新词。

用法:
    python3 trend_watch.py

功能:
    1. Google Trends（主数据源）：抓取美国区（US）trending searches，获取24小时内热度飙升词；
       并对 top 候选词通过 interest_over_time 验证过去 7 天热度曲线，确认是'突然冒头'而非'长期热词'。
    2. Reddit rising（辅助）：抓取 r/all/rising 帖子标题，提取 2-4 词关键词短语。
    3. X (Twitter) 趋势：无公开免费 API，标记为 TODO 跳过。
    4. 过滤与分类：过滤纯人名八卦与无明确意图词，保留工具型（tool）或资讯型（info）意图的词。
    5. 输出：生成 ~/workspace/trend-monitor/outputs/YYYY-MM-DD-HH.json（北京时间命名）。
"""

import os
import sys
import re
import time
import json
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from collections import Counter

# 解决本机 no_proxy 环境变量中方括号 IPv6 条目导致 httpx / requests 解析崩溃的问题
os.environ['no_proxy'] = 'localhost,127.0.0.1,::1'
os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1'

import requests
import pandas as pd
from pytrends.request import TrendReq

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
# 数据源 1: Google Trends
# ---------------------------------------------------------------------------
def fetch_google_trends():
    """抓取美国区（US）trending searches，并通过 interest_over_time 验证突发性"""
    results = []
    print("[Google Trends] 开始抓取 US trending searches...")

    raw_trends = []
    # 优先尝试 pytrends.trending_searches，若端点变动失败则通过官方 RSS 兜底
    pt = TrendReq(hl='en-US', tz=360)
    try:
        df = pt.trending_searches(pn='united_states')
        for item in df[0].tolist():
            raw_trends.append({'term': str(item), 'traffic': 'N/A', 'news': []})
        print(f"[Google Trends] 通过 pytrends.trending_searches 获取到 {len(raw_trends)} 个词")
    except Exception as e:
        print(f"[Google Trends] pytrends.trending_searches 暂不可用 ({e})，使用 Google Trends 官方 RSS 源...")
        try:
            r = requests.get('https://trends.google.com/trending/rss?geo=US', timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ns = {'ht': 'https://trends.google.com/trending/rss'}
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ''
                traffic = item.find('ht:approx_traffic', ns)
                traffic_text = traffic.text if traffic is not None else 'N/A'
                news_items = [
                    n.find('ht:news_item_title', ns).text
                    for n in item.findall('ht:news_item', ns)
                    if n.find('ht:news_item_title', ns) is not None and n.find('ht:news_item_title', ns).text
                ]
                raw_trends.append({
                    'term': title,
                    'traffic': traffic_text,
                    'news': news_items
                })
            print(f"[Google Trends] 通过官方 RSS 获取到 {len(raw_trends)} 个趋势词")
        except Exception as rss_err:
            print(f"[Google Trends] 抓取 RSS 失败: {rss_err}")
            return results

    # 对候选词进行初筛
    candidates = []
    for t in raw_trends:
        term = t['term']
        context = ' '.join(t['news'])
        valid, reason = check_candidate(term, context)
        if valid:
            candidates.append(t)
        else:
            print(f"[Google Trends] 过滤掉{reason}: {term}")

    # 对前 5 个候选词使用 pytrends.interest_over_time 验证过去 7 天曲线，确认是否为'突然冒头'
    top_candidates = candidates[:5]
    for idx, item in enumerate(top_candidates):
        term = item['term']
        traffic = item['traffic']
        news = item['news']
        context = ' '.join(news)

        curve_note = ""

        try:
            time.sleep(1.2)  # 避免触发 Google 429 限流
            pt_check = TrendReq(hl='en-US', tz=360)
            pt_check.build_payload([term], timeframe='now 7-d', geo='US')
            iot_df = pt_check.interest_over_time()

            if not iot_df.empty and term in iot_df.columns:
                series = iot_df[term].tolist()
                if len(series) >= 24:
                    baseline = sum(series[:-24]) / max(len(series[:-24]), 1)
                    recent_peak = max(series[-24:])
                    
                    # 判断是否为长期高热度词（基线很高且无明显相对飙升）
                    if baseline >= 45 and recent_peak <= baseline * 1.5:
                        print(f"[Google Trends] {term} 过去7天基线高 ({baseline:.1f})，属长期热词，跳过")
                        continue
                    else:
                        curve_note = f"7天基线热度: {baseline:.1f}, 24h峰值: {recent_peak} (确认突然冒头)"
                else:
                    curve_note = "7天曲线数据点较少，经24h热榜验证突发"
            else:
                curve_note = "7天曲线平稳无历史积累，24h内首次爆发"
        except Exception as iot_err:
            curve_note = f"7天曲线校验跳过({type(iot_err).__name__})，依赖24h榜单确认"

        # 推测飙升原因 note
        reason_parts = []
        if curve_note:
            reason_parts.append(curve_note)
        if news:
            reason_parts.append(f"相关事件: {news[0]}")
        else:
            reason_parts.append("24小时内搜索量大幅异动")

        intent = classify_intent(term, context)
        results.append({
            "term": term,
            "source": "google_trends",
            "heat": traffic,
            "intent": intent,
            "note": "；".join(reason_parts)
        })

    print(f"[Google Trends] 抓取完成，产出 {len(results)} 个合格新词")
    return results


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

        results.append({
            "term": phrase,
            "source": "reddit",
            "heat": score,
            "intent": intent,
            "note": f"Reddit社区上升热帖高频词，原帖: \"{sample_title[:60]}...\""
        })
        if len(results) >= 5:
            break

    print(f"[Reddit] 抓取完成，产出 {len(results)} 个合格新词")
    return results


# ---------------------------------------------------------------------------
# 数据源 3: X 趋势 (TODO)
# ---------------------------------------------------------------------------
def fetch_x_trends():
    """X 趋势数据源：无公开免费 API，标记为 TODO 跳过"""
    # TODO: X (Twitter) 没有公开免费 API，按要求跳过，避免硬爬引起不稳定
    return []


# ---------------------------------------------------------------------------
# 主流程与文件输出
# ---------------------------------------------------------------------------
def main():
    print("=== 开始运行搜索新词监控 (trend_watch.py) ===")
    all_candidates = []

    # 1. Google Trends (主数据源)
    try:
        gt_results = fetch_google_trends()
        all_candidates.extend(gt_results)
    except Exception as e:
        print(f"[Google Trends] 处理发生未知异常: {e}")

    # 2. Reddit rising (辅助)
    try:
        reddit_results = fetch_reddit_rising()
        all_candidates.extend(reddit_results)
    except Exception as e:
        print(f"[Reddit] 处理发生未知异常: {e}")

    # 3. X 趋势 (TODO)
    try:
        x_results = fetch_x_trends()
        all_candidates.extend(x_results)
    except Exception as e:
        print(f"[X] 处理发生未知异常: {e}")

    # 确定输出文件路径（按北京时间 UTC+8 命名：YYYY-MM-DD-HH.json）
    beijing_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(timezone.utc).astimezone(beijing_tz)
    file_name = now_bj.strftime("%Y-%m-%d-%H.json")

    output_dir = os.path.expanduser("~/workspace/trend-monitor/outputs")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, file_name)

    # 写入 JSON 文件
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_candidates, f, ensure_ascii=False, indent=2)

    print(f"\n=== 监控完成 ===")
    print(f"有效候选新词总数: {len(all_candidates)}")
    print(f"输出文件路径: {output_path}")
    print("\n--- 结果预览 ---")
    print(json.dumps(all_candidates, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

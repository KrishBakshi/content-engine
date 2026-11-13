"""What is trending in one run, worked out from the item dicts alone. No LLM.

A topic trends when several independent sources are on it. Two ways that shows up:

1. The same name in the titles of items from unrelated sources ("Jensen Huang" on Reddit,
   in a newsletter and on a trade site).
2. The same link carried by more than one source (a blog post that is also on Hacker
   News); the engine records that as `also_in` on the item.

Engagement numbers (Hacker News points and comments) only break ties and fill the "most
discussed" list: they exist for few sources, so they can't be the main signal.

Names, not words. A first attempt matched any shared title word and produced topics like
"search" and "builds". Here a word counts as a name only if the feeds themselves write it
like one: capitalised in the middle of a sentence more often than not, or with an inner
capital or a digit (NVIDIA, GPT-5). Headlines in Title Case teach nothing about which
words are names, so the vocabulary is learned from sentence-case titles and summaries
and then looked up in every title.

A source writing about itself is not a signal: NVIDIA's blog naming NVIDIA, Anthropic's
posts naming Claude. Those items can sit inside a topic but don't count towards it.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from content_engine.schema import source_family

MAX_TOPICS = 5
MAX_DISCUSSED = 3
MIN_DISCUSSED_POINTS = 50
MERGE_OVERLAP = 0.6   # topics sharing this much of the smaller one's items are the same story
SUMMARY_SCAN = 160    # a summary's opening is its subject; further in, names are passing mentions
# Independent sources a topic needs. A two-word name ("Jensen Huang") is precise, so two
# sources are enough. One word is not: product names capitalise ordinary words ("Agent
# Framework", "Together Compiler"), and two sources sharing one of those is coincidence.
MIN_SOURCES = {1: 3, 2: 2}

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+']*")
_VERSION = re.compile(r"v?\d+([.]\d+)*[a-z]?\d*", re.I)
_NUMBER = re.compile(r"\d{1,2}([.]\d{1,2})?")  # "4", "5.5": a model's version, as in "Gemini 4"
_SENTENCE_END = re.compile(r"[.!?:]\s+|\n")

# Capitalised often, but too common to be a topic.
GENERIC = frozenset("""ai llm llms api apis gpu gpus cpu ceo cto cfo usa hn show ask tell pdf ml rl nlp sota oss rag agi
os ui sdk cli json http https sql python rust java javascript typescript linux macos windows docker kubernetes aws
azure gcp cuda github arxiv reddit youtube twitter today monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december introducing announcing new
how why what when the this that these with from for and but not our your their part episode day week year release
released update updates blog post paper preprint abstract read more via using open source model models agent agents
data code video show hn""".split())
# Real names, but so broad that sharing one says little: fine as a second label on a topic,
# never enough to start one.
BROAD = frozenset("""google microsoft openai anthropic meta nvidia apple amazon ibm intel amd tesla samsung china
chinese india japan korea europe european america american us uk eu russia claude gemini gpt chatgpt copilot""".split())
# What each source is entitled to say about itself without it counting as a mention.
OWN_NAMES = {
    "nvidia": {"nvidia"}, "anthropic": {"anthropic", "claude"}, "openai_news": {"openai", "gpt", "chatgpt", "codex"},
    "openai_agents_sdk_releases": {"openai"}, "google_research": {"google", "gemini"}, "meta_engineering": {"meta"},
    "cloudflare_blog": {"cloudflare"}, "langchain_blog": {"langchain", "langgraph", "langsmith"},
    "github_blog_ai": {"github", "copilot"}, "microsoft_agent_framework": {"microsoft"},
}
FAMILY_LABELS = {"hacker_news": "Hacker News", "reddit": "Reddit", "arxiv": "arXiv", "github_new": "GitHub",
                 "anthropic": "Anthropic", "nvidia": "NVIDIA", "huggingface_papers": "Hugging Face papers",
                 "techmeme": "Techmeme", "lobsters_ai": "Lobsters", "openai_news": "OpenAI",
                 "ieee_spectrum_robotics": "IEEE Spectrum"}


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family) or family.replace("_", " ").title()


def _words(text: str) -> list[str]:
    """Tokens with possessives and trailing dots removed; hyphens split words ("llm-gemini")."""
    out = []
    for raw in _WORD.findall(text.replace("’", "'").replace("-", " ")):
        word = re.sub(r"'s$", "", raw).strip(".'")
        if word:
            out.append(word)
    return out


def _is_title_case(words: list[str]) -> bool:
    long = [w for w in words if len(w) > 3 and w[0].isalpha()]
    return len(long) >= 3 and sum(w[0].isupper() for w in long) / len(long) >= 0.7


def learn_names(items: list[dict]) -> set[str]:
    """Lowercased words the feeds write as names.

    A capital letter mid-sentence is the main evidence. One at the start of a sentence proves
    nothing alone, but a word that starts at least two different items' sentences and is never
    written lowercase anywhere in the run is a name too ("Jensen Huang resigns...")."""
    capital, lower, marked = Counter(), Counter(), set()
    started: dict[str, set[int]] = {}
    for index, item in enumerate(items):
        for text in (item["title"], item.get("summary", "")):
            for sentence in _SENTENCE_END.split(text):
                words = _words(sentence)
                title_case = _is_title_case(words)
                for position, word in enumerate(words):
                    key = word.lower()
                    if len(key) < 3 or key in GENERIC or _VERSION.fullmatch(word):
                        continue
                    letters = sum(c.isalpha() for c in word)
                    if word[0].isalpha() and letters >= 3 and any(c.isupper() for c in word[1:]):
                        marked.add(key)  # NVIDIA, LocalLLaMA, GPT5: a name wherever it stands
                    elif any(c.isdigit() for c in word):
                        continue         # commit hashes, sizes, version fragments
                    elif title_case:
                        continue         # capital proves nothing here
                    elif position == 0:
                        if word[0].isupper():
                            started.setdefault(key, set()).add(index)
                        else:
                            lower[key] += 1
                    elif word[0].isupper():
                        capital[key] += 1
                    else:
                        lower[key] += 1
    return (marked | {k for k, n in capital.items() if n > lower[k]}
            | {k for k, where in started.items() if len(where) >= 2 and lower[k] == 0})


def _points(metric: str) -> tuple[int, int]:
    points, comments = re.search(r"(\d+) pts", metric), re.search(r"(\d+) comments", metric)
    return (int(points.group(1)) if points else 0, int(comments.group(1)) if comments else 0)


def _voices(item: dict) -> list[tuple[str, str]]:
    """(family, metric) for the item's own source and every other source that carried its link."""
    return [(source_family(item["source"]), item["metric"])] + [
        (source_family(a["source"]), a["metric"]) for a in item.get("also_in", [])]


def _is_self_mention(item: dict, key_words: set[str]) -> bool:
    family = source_family(item["source"])
    own = OWN_NAMES.get(family) or OWN_NAMES.get(item["source"]) or set()
    return bool(key_words & own)


def _topic(label: str, indexes: list[int], items: list[dict], counted: set[int]) -> dict:
    families, points, comments = [], 0, 0
    for i in indexes:
        for family, metric in _voices(items[i]):
            p, c = _points(metric)
            points, comments = points + p, comments + c
            if i in counted and family not in families:
                families.append(family)
    mentions = sum(len(_voices(items[i])) for i in indexes)
    # Lead with the items people are discussing, then keep the brief's own order.
    ordered = sorted(indexes, key=lambda i: (-max(_points(m)[0] for _, m in _voices(items[i])), i))
    return {
        "label": label,
        "mentions": mentions,
        "sources": [family_label(f) for f in families],
        "points": points,
        "comments": comments,
        "score": round(3 * len(families) + mentions + math.log1p(points), 2),
        "urls": [items[i]["url"] for i in ordered],
    }


def find_trending(items: list[dict]) -> dict:
    """{"topics": [...], "most_discussed": [urls]} for one run's items (as in items.json)."""
    names = learn_names(items)

    # Every name, and every pair of adjacent names, mapped to the items that carry it. Single
    # names are looked for in titles only (a summary mentions many things in passing); a
    # two-word name is precise enough to be matched at the start of the summary as well.
    occurrences: dict[str, list[int]] = {}
    display: dict[str, str] = {}
    for index, item in enumerate(items):
        keys = {}
        for text, singles in ((item["title"], True), (item.get("summary", "")[:SUMMARY_SCAN], False)):
            for sentence in _SENTENCE_END.split(text):
                words = _words(sentence)
                is_name = [w.lower() in names and w.lower() not in GENERIC for w in words]
                for position, word in enumerate(words):
                    if not is_name[position]:
                        continue
                    if singles:
                        keys.setdefault(word.lower(), word)
                    if position + 1 < len(words) and (is_name[position + 1] or _NUMBER.fullmatch(words[position + 1])):
                        keys.setdefault(f"{word.lower()} {words[position + 1].lower()}", f"{word} {words[position + 1]}")
        for key, shown in keys.items():
            occurrences.setdefault(key, []).append(index)
            # Prefer a spelling that isn't shouting or all lowercase.
            if key not in display or (display[key].isupper() or display[key].islower()) and not shown.isupper():
                display[key] = shown

    def counted(key: str, indexes: list[int]) -> set[int]:
        return {i for i in indexes if not _is_self_mention(items[i], set(key.split()))}

    def strength(key: str) -> tuple:
        indexes = occurrences[key]
        families = {f for i in counted(key, indexes) for f, _ in _voices(items[i])}
        return (all(w in BROAD for w in key.split()), -len(key.split()), -len(families), -len(indexes), key)

    groups: list[dict] = []
    for key in sorted(occurrences, key=strength):
        indexes = occurrences[key]
        independent = counted(key, indexes)
        broad = all(w in BROAD for w in key.split())
        words = set(key.split())
        # Only a piece of a topic's own name folds into it ("Jensen" into "Jensen Huang"), and
        # only when it is the same story. Any other shared name, even one most of the topic's
        # items carry, starts its own candidate: the word "Times" is in "New York Times" and in
        # "EE Times", and that does not make them one story.
        home = next((g for g in groups if independent and words <= set(g["keys"][0].split())
                     and len(independent & g["items"]) >= MERGE_OVERLAP * len(independent)), None)
        context = next((g for g in groups if broad and independent
                        and len(independent & g["items"]) >= MERGE_OVERLAP * len(independent)), None)
        if home:
            home["items"] |= set(indexes)
            home["counted"] |= {i for i in independent if not _is_self_mention(items[i], set(home["keys"][0].split()))}
        elif context:
            # "NVIDIA" names the Jensen Huang story's setting; its other items are unrelated.
            # Not worth saying when the topic's own name already contains it.
            if not words <= set(context["keys"][0].split()):
                context["context"] = context.get("context") or key
        elif not broad:
            groups.append({"keys": [key], "items": set(indexes), "counted": set(independent)})

    topics, used = [], set()
    for group in groups:
        key = group["keys"][0]
        label = display[key] + (f" · {display[group['context']]}" if group.get("context") else "")
        topic = _topic(label, sorted(group["items"]), items, group["counted"])
        if len(topic["sources"]) >= MIN_SOURCES[len(key.split())]:
            topics.append(topic)
    # A single link that several sources carried is a topic too, named by its title.
    grouped = {url for t in topics for url in t["urls"]}
    for index, item in enumerate(items):
        if item["url"] not in grouped and len({f for f, _ in _voices(item)}) >= 2:
            topics.append(_topic(item["title"], [index], items, {index}))

    topics.sort(key=lambda t: (-t["score"], t["label"]))
    # One story can surface under several names ("Jensen Huang", "Colette Kress"): keep the
    # strongest and drop a later topic that is mostly the same items.
    distinct: list[dict] = []
    for topic in topics:
        mine = set(topic["urls"])
        if not any(len(mine & set(kept["urls"])) >= MERGE_OVERLAP * len(mine) for kept in distinct):
            distinct.append(topic)
    topics = distinct[:MAX_TOPICS]
    used = {url for t in topics for url in t["urls"]}

    discussed = sorted(((max(_points(m)[0] for _, m in _voices(item)), -index, item["url"])
                        for index, item in enumerate(items) if item["url"] not in used), reverse=True)
    return {"topics": topics,
            "most_discussed": [url for points, _, url in discussed[:MAX_DISCUSSED] if points >= MIN_DISCUSSED_POINTS]}

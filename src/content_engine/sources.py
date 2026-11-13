"""The source registry. Adding a source = one entry here (and a collector class if it isn't a feed).

Deviations from the spec's registry (docs/project_engine_instructions.md, section 3),
found by probing every endpoint on 2026-11-10:
- Reddit `hot.json` returns 403 to unauthenticated clients; `hot.rss` (Atom) works but has
  no score, so Reddit items carry metric "hot" instead of points. Both subreddits come from
  one request (see collectors/reddit.py for the rate limit).
- Anthropic publishes no feed (`/feed.xml`, `/rss.xml`, `/news/rss.xml` are all 404): not included.
- `ai.meta.com/blog/rss.xml` is 404; Meta's engineering blog feed is used instead.
- llama.cpp moved from `ggerganov/` to `ggml-org/`.

Added beyond the spec on 2026-11-13, at the user's request for more on backend agent AI
and physical AI (categories `agents` and `physical_ai`). Every feed below was fetched once
with the engine's User-Agent and parsed with parse_feed before being listed. Tried and
left out: Boston Dynamics (feed exists but has no items), Physical Intelligence, Figure,
Agility, Skydio (no feed), Toyota Research (descriptions are byline/date boilerplate),
AWS ML blog (about ten posts a day, mostly product), Hugging Face and DeepMind blogs
(not specific to either topic). Added and then removed as redundant after a live run:
InfoQ AI (general cloud news; The New Stack covers the agent side better), TechCrunch
robotics (repeats The Robot Report's stories), Waymo blog (community and PR posts),
Roboflow blog (computer-vision product how-tos, not robotics). Simon Willison's blog was
removed once and put back: the user looked at it and wants it.
"""

from __future__ import annotations

from content_engine.collectors.api import GitHubSearchCollector, HackerNewsCollector, HuggingFacePapersCollector
from content_engine.collectors.base import BaseCollector
from content_engine.collectors.feed import FeedCollector
from content_engine.collectors.reddit import RedditCollector


def default_collectors() -> list[BaseCollector]:
    # Registry order matters twice: it is the order inside each section of the brief, and
    # when two sources carry the same URL the earlier one keeps it. The focus topics come
    # first so a story about agents lands under Agent AI, not under general community news.
    return [
        # --- agents: building and running AI agents on the backend ---
        HackerNewsCollector("hacker_news_agents", words="agent agents agentic MCP", min_points=25, limit=6,
                            category="agents", title_only=True, empty_ok=True),
        FeedCollector("langchain_blog", "agents", "https://www.langchain.com/blog/rss.xml", 3, "Blog"),
        FeedCollector("github_blog_ai", "agents", "https://github.blog/ai-and-ml/feed/", 3, "Blog"),
        FeedCollector("microsoft_agent_framework", "agents", "https://devblogs.microsoft.com/agent-framework/feed/",
                      2, "Blog", max_age_days=30),
        FeedCollector("latent_space", "agents", "https://www.latent.space/feed", 3, "Newsletter"),
        FeedCollector("simon_willison", "agents", "https://simonwillison.net/atom/everything/", 4, "Blog"),
        FeedCollector("the_new_stack_ai", "agents", "https://thenewstack.io/category/ai/feed/", 3, "News"),
        FeedCollector("arxiv_cs_ma", "agents", "https://rss.arxiv.org/atom/cs.MA", 4, "Preprint", empty_ok=True),
        GitHubSearchCollector("github_new_agent_repos", "topic:ai-agents created:>{since} stars:>20", limit=4,
                              category="agents"),
        # Release feeds are capped by age: the MCP spec ships about twice a year. LangGraph and
        # the Claude Agent SDK were tried and left out: their notes are dependency bumps and a
        # bare "pip install" line.
        FeedCollector("openai_agents_sdk_releases", "agents",
                      "https://github.com/openai/openai-agents-python/releases.atom", 2, "Release", max_age_days=14),
        FeedCollector("mcp_spec_releases", "agents",
                      "https://github.com/modelcontextprotocol/modelcontextprotocol/releases.atom", 2, "Release",
                      max_age_days=45),
        # --- physical_ai: robotics, embodied AI, vision for machines ---
        # Robotics reaches the front page less often, so look back three days.
        HackerNewsCollector("hacker_news_robotics", words="robot robots robotics humanoid", hours_back=72,
                            min_points=20, limit=5, category="physical_ai", title_only=True, empty_ok=True),
        FeedCollector("nvidia_robotics", "physical_ai", "https://blogs.nvidia.com/blog/category/robotics/feed/",
                      3, "Blog", max_age_days=30),
        FeedCollector("nvidia_developer_robotics", "physical_ai",
                      "https://developer.nvidia.com/blog/category/robotics/feed/", 3, "Blog", max_age_days=30),
        FeedCollector("the_robot_report", "physical_ai", "https://www.therobotreport.com/feed/", 5, "News"),
        FeedCollector("ieee_spectrum_robotics", "physical_ai", "https://spectrum.ieee.org/feeds/topic/robotics.rss",
                      3, "News", max_age_days=30),
        FeedCollector("robohub", "physical_ai", "https://robohub.org/feed/", 3, "News", max_age_days=30),
        FeedCollector("weekly_robotics", "physical_ai", "https://www.weeklyrobotics.com/atom.xml", 1, "Newsletter",
                      max_age_days=8),
        FeedCollector("arxiv_cs_ro", "physical_ai", "https://rss.arxiv.org/atom/cs.RO", 5, "Preprint", empty_ok=True),
        GitHubSearchCollector("github_new_robotics_repos", "topic:robotics created:>{since} stars:>10", limit=4,
                              category="physical_ai"),
        FeedCollector("lerobot_releases", "physical_ai", "https://github.com/huggingface/lerobot/releases.atom",
                      1, "Release", max_age_days=30),
        FeedCollector("isaac_lab_releases", "physical_ai", "https://github.com/isaac-sim/IsaacLab/releases.atom",
                      1, "Release", max_age_days=30),
        # --- community ---
        HackerNewsCollector(words="AI LLM", hours_back=24, min_points=25, limit=10),
        # One request for all subreddits (see collectors/reddit.py); each lands in its own section.
        RedditCollector({"LocalLLaMA": "community", "MachineLearning": "community",
                         "AI_Agents": "agents", "robotics": "physical_ai"}, per_sub=8),
        FeedCollector("lobsters_ai", "community", "https://lobste.rs/t/ai.rss", 6, "Discussion"),
        FeedCollector("techmeme", "community", "https://www.techmeme.com/feed.xml", 6, "News"),
        # research (arXiv feeds are empty on days with no announcement, e.g. weekends)
        HuggingFacePapersCollector(limit=8),
        FeedCollector("arxiv_cs_ai", "research", "https://rss.arxiv.org/atom/cs.AI", 5, "Preprint", empty_ok=True),
        FeedCollector("arxiv_cs_lg", "research", "https://rss.arxiv.org/atom/cs.LG", 5, "Preprint", empty_ok=True),
        FeedCollector("arxiv_cs_cl", "research", "https://rss.arxiv.org/atom/cs.CL", 5, "Preprint", empty_ok=True),
        # tooling
        GitHubSearchCollector(),
        FeedCollector("llama_cpp_releases", "tooling", "https://github.com/ggml-org/llama.cpp/releases.atom", 3, "Release"),
        FeedCollector("vllm_releases", "tooling", "https://github.com/vllm-project/vllm/releases.atom", 3, "Release"),
        # engineering_blogs
        FeedCollector("google_research", "engineering_blogs", "https://research.google/blog/rss/", 4, "Blog"),
        FeedCollector("openai_news", "engineering_blogs", "https://openai.com/news/rss.xml", 4, "Blog"),
        FeedCollector("meta_engineering", "engineering_blogs", "https://engineering.fb.com/feed/", 4, "Blog"),
        FeedCollector("cloudflare_blog", "engineering_blogs", "https://blog.cloudflare.com/rss/", 4, "Blog"),
    ]

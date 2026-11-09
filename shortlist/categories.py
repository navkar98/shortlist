"""Group jobs by what their titles say: the career stage they are for and the field they are in.

Title words only, matched as whole words or phrases (as the filters match), so every group is easy to check. A title
can carry several groups ("PhD New Grad, Machine Learning" is both a new-grad and an ML role), or none. The web app
filters matches by these groups; a daily list of open roles can use the same ones.
"""
import re
from functools import lru_cache

# group -> (label, words in the title). Within each kind, the order is the order shown.
STAGES = {
    "new-grad": ("New grad", ["new grad", "new grads", "new graduate", "graduate", "graduates", "university grad", "university graduate",
                              "recent graduate", "recent grad", "early career", "entry level", "entry-level", "junior",
                              "campus", "graduate program", "graduate programme", "graduate scheme", "grad program",
                              "2025 start", "2026 start", "2027 start", "2028 start"]),
    "phd": ("PhD & postdoc", ["phd", "ph.d", "ph.d.", "doctoral", "postdoc", "postdoctoral", "post-doctoral",
                              "post doctoral", "residency", "resident"]),
    "intern": ("Intern", ["intern", "interns", "internship", "internships", "co-op", "coop", "apprentice",
                          "apprenticeship", "student", "working student", "placement", "summer analyst"]),
    "senior": ("Senior", ["senior", "sr", "sr.", "staff", "principal", "lead", "distinguished", "director", "head"]),
}
FIELDS = {
    "research": ("Research", ["research scientist", "research engineer", "researcher", "research", "postdoc",
                              "postdoctoral", "post-doctoral", "scientist, research"]),
    "ml": ("ML & AI", ["machine learning", "ml", "mle", "deep learning", "ai", "a.i.", "artificial intelligence",
                       "applied scientist", "applied science", "llm", "llms", "nlp", "natural language", "computer vision",
                       "generative ai", "genai", "recommendation", "recommendations", "recommender", "recsys", "ranking",
                       "personalization", "personalisation", "relevance", "reinforcement learning", "perception",
                       "speech", "multimodal", "foundation model", "foundation models", "inference", "ai engineer"]),
    "data": ("Data", ["data scientist", "data science", "data engineer", "data engineering", "analytics engineer",
                      "statistician", "statistics", "decision scientist", "quantitative analyst"]),
    "quant": ("Quant", ["quant", "quantitative", "quantitative researcher", "quantitative developer", "trader",
                        "trading", "algorithmic trading"]),
    "software": ("Software", ["software engineer", "software engineering", "software developer", "software development",
                              "swe", "sde", "developer", "backend", "back-end", "back end", "frontend", "front-end",
                              "full stack", "full-stack", "fullstack", "platform engineer", "infrastructure engineer",
                              "devops", "site reliability", "programmer", "mobile engineer", "ios", "android"]),
    "hardware": ("Hardware", ["hardware", "asic", "fpga", "rtl", "silicon", "chip", "soc", "embedded", "firmware",
                              "circuit", "analog", "electrical engineer", "verification engineer", "physical design",
                              "gpu architecture", "cpu", "photonics", "robotics"]),
    "product": ("Product", ["product manager", "product management", "product owner", "program manager",
                            "technical program manager", "tpm"]),
}
LABELS = {group: label for kind in (STAGES, FIELDS) for group, (label, _) in kind.items()}


def _pattern(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(words, key=len, reverse=True))) + r")(?!\w)", re.I)


_PATTERNS = {group: _pattern(words) for kind in (STAGES, FIELDS) for group, (_, words) in kind.items()}


@lru_cache(maxsize=65536)
def groups(title: str) -> tuple[str, ...]:
    """The groups a job title belongs to, stages first, each in display order."""
    return tuple(group for group, pattern in _PATTERNS.items() if pattern.search(title or ""))

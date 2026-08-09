"""Run the DeepSeek + Claude subscription current-news council.

Prerequisites:
  - DEEPSEEK_API_KEY
  - authenticated Claude Code CLI (Claude.ai subscription login)
  - LAZYCRAWLER_NEWS_DB or an explicit news_db path
"""

from __future__ import annotations

import os

from lazytools.skills import deepseek_claude_news_council


def main() -> None:
    question = os.environ.get(
        "COUNCIL_QUESTION",
        "What current development deserves the most attention, and why?",
    )
    result = deepseek_claude_news_council(question).run()
    print(result.text())
    print(f"\nQuorum reached: {result.quorum_reached}")


if __name__ == "__main__":
    main()

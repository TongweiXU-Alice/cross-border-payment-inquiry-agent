"""Small interactive CLI for the same workflow used by the Streamlit UI."""

from setup_db import DB, build
from agent import run_agent


def main() -> int:
    if not DB.exists():
        build()
    print("Cross-border Payment Inquiry Agent")
    print("Synthetic local demo · type exit to quit")
    while True:
        query = input("\nInquiry > ").strip()
        if query.lower() in {"exit", "quit"}:
            return 0
        if not query:
            print("Please enter an inquiry.")
            continue
        result = run_agent(query)
        print("\nDecision:", result.decision)
        print("Understanding mode:", result.understanding_mode)
        print("Human review:", "YES" if result.human_review_required else "NO")
        print("\nDraft:\n", result.draft_response)
        print("\nTool trace:")
        for event in result.tool_trace:
            print(
                "{step}. {name} [{status}] — {summary}".format(
                    step=event["step"],
                    name=event["name"],
                    status=event["status"],
                    summary=event["summary"],
                )
            )


if __name__ == "__main__":
    raise SystemExit(main())

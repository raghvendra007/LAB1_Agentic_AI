NAME = "finish"
ARGS = {"summary": str}
DESCRIPTION = 'args {"summary": "..."}  Stop and report. Only accepted when your last page passed ALL checks.'


def run(args, state):
    """The stop signal. The gate: the last write_site must have passed every check."""
    if state.last_report is None:
        return "Rejected: write the page (again) first.", "finish rejected: no checked page"
    if not state.last_report.passed:
        failed = state.last_report.summary(only_failures=True)
        return f"Rejected: the last page has failures:\n{failed}", "finish rejected: checks failing"
    state.done = True
    print(f"\n  FINISHED: {args['summary']}")
    return "Accepted.", "finish accepted"
